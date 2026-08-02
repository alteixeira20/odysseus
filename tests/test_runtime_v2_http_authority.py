"""HTTP-backed authority preparation for the production chat routes."""

from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

import routes.chat_routes as chat_routes
from routes.chat_helpers import ChatContext, PreprocessedMessage, PresetInfo
from src.agent.rounds.tool_calls import normalize_tool_calls
from src.agent.runtime_v2.executor import execute_normalized_tool_call
from src.agent.runtime_v2.approvals import EFFECT_APPROVALS
from src.agent.runtime_v2.events import encode_runtime_sse
from src.agent.runtime_v2.state import RunState
from src.agent.tools.bootstrap import TOOL_REGISTRY
from src.agent_tools import ToolBlock
from src.execution_policy import ExecutionMode


@dataclass
class _Session:
    model: str = "test-model"
    endpoint_url: str = "http://provider.invalid/v1"
    headers: dict = None
    history: list = None
    name: str = "Runtime V2 HTTP test"

    def __post_init__(self):
        self.headers = {}
        self.history = []


class _SessionManager:
    def __init__(self):
        self.session = _Session()

    def get_session(self, session_id):
        if session_id != "runtime-v2-http":
            raise KeyError(session_id)
        return self.session

    def get_session_snapshot(self, session_id):
        return copy.deepcopy(self.get_session(session_id))

    def replace_messages(self, session_id, messages):
        self.get_session(session_id).history = list(messages)
        return True

    def save_sessions(self):
        return None


class _EmptyQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return None


class _EmptyDatabase:
    def query(self, *args, **kwargs):
        return _EmptyQuery()

    def close(self):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None


def _execute(context, name, arguments):
    call = normalize_tool_calls(
        [ToolBlock(name, "", arguments=arguments)],
        [],
        execution_context=context,
        provider_name="http-authority-test",
    )[0]
    return asyncio.run(execute_normalized_tool_call(call, context))


def _execute_approved(context, name, arguments):
    call = normalize_tool_calls(
        [ToolBlock(name, "", arguments=arguments)],
        [],
        execution_context=context,
        provider_name="http-authority-approved-test",
    )[0]

    async def execute():
        waiting = await execute_normalized_tool_call(call, context)
        assert waiting.status.value == "approval_required"
        approval_id = waiting.data["approval"]["approval_id"]
        EFFECT_APPROVALS.decide(
            approval_id,
            owner_id=context.owner_id,
            decision="allow",
        )
        return await execute_normalized_tool_call(
            call,
            context,
            approval_id=approval_id,
        )

    return asyncio.run(execute())


def test_production_http_preparation_binds_roots_and_one_run_host_authority(
    monkeypatch,
    tmp_path,
):
    session_manager = _SessionManager()
    principal = {"name": "admin", "admin": True}
    captured_contexts = []
    runs = {}
    approval_ready = threading.Event()
    approval_capture = {}

    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_routes, "get_current_user", lambda request: principal["name"])
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: principal["name"])
    monkeypatch.setattr(chat_routes, "_reconcile_selected_route_from_request", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_routes, "_clear_orphaned_session_endpoint", lambda *args, **kwargs: False)
    monkeypatch.setattr(chat_routes, "_recover_empty_session_model", lambda *args, **kwargs: False)
    monkeypatch.setattr(chat_routes, "_enforce_chat_privileges", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_routes, "resolve_session_auth", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_routes, "get_session_mode", lambda *args, **kwargs: "agent")
    monkeypatch.setattr(chat_routes, "set_session_mode", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_routes, "_is_image_generation_session", lambda *args, **kwargs: False)
    monkeypatch.setattr(chat_routes, "SessionLocal", _EmptyDatabase)
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user",
        lambda owner: principal["admin"] and owner == principal["name"],
    )
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)

    async def build_context(*args, **kwargs):
        message = kwargs["message"]
        args[0].history.append(
            SimpleNamespace(role="user", content=message, metadata=None)
        )
        return ChatContext(
            preface=[],
            rag_sources=[],
            web_sources=[],
            used_memories=[],
            messages=[{"role": "user", "content": message}],
            context_length=32,
            was_compacted=False,
            user=principal["name"],
            uprefs={},
            preset=PresetInfo(0.0, 64, None, None),
            preprocessed=PreprocessedMessage(message, message, message, [], []),
        )

    monkeypatch.setattr(chat_routes, "build_chat_context", build_context)

    async def agent_stream(*args, execution_context, **kwargs):
        latest = str(args[2][-1].get("content") or "")
        yield encode_runtime_sse(
            execution_context.event_factory.create(
                "run_state",
                {"state": RunState.PREPARING.value, "reason": "http_prepared"},
            )
        )
        yield encode_runtime_sse(
            execution_context.event_factory.create(
                "run_state",
                {"state": RunState.RUNNING.value, "reason": "http_running"},
            )
        )
        if "Search continuation" in latest:
            continuation = None
            for page_number in range(10):
                arguments = {
                    "pattern": "http-needle",
                    "fixed_string": True,
                    "max_results": 1,
                    "max_scan_entries": 1,
                }
                if continuation:
                    arguments["continuation"] = continuation["token"]
                call = normalize_tool_calls(
                    [ToolBlock("search_text", "", arguments=arguments)],
                    [],
                    execution_context=execution_context,
                    provider_name="http-sse-continuation-test",
                )[0]
                yield encode_runtime_sse(
                    execution_context.event_factory.create(
                        "tool_started",
                        {
                            "call_id": call.call_id,
                            "canonical_name": call.canonical_name,
                            "raw_name": call.raw_name,
                            "round": page_number + 1,
                        },
                        caused_by=call.call_id,
                    )
                )
                result = await execute_normalized_tool_call(
                    call,
                    execution_context,
                )
                yield encode_runtime_sse(
                    execution_context.event_factory.create(
                        "tool_result",
                        {
                            "result": result.as_dict(),
                            "round": page_number + 1,
                        },
                        caused_by=call.call_id,
                    )
                )
                continuation = result.continuation
                if continuation is None:
                    break
            else:
                raise AssertionError("HTTP search continuation did not terminate")
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "run_state",
                    {
                        "state": RunState.COMPLETED.value,
                        "disposition": "completed",
                        "reason": "http_search_continuation_complete",
                        "resumable": False,
                        "terminal": True,
                    },
                )
            )
        elif "Delete obsolete" in latest:
            call = normalize_tool_calls(
                [
                    ToolBlock(
                        "patch_workspace",
                        "",
                        arguments={
                            "operations": [
                                {
                                    "type": "delete",
                                    "path": "obsolete-http.txt",
                                }
                            ]
                        },
                    )
                ],
                [],
                execution_context=execution_context,
                provider_name="http-sse-approval-test",
            )[0]
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "tool_started",
                    {
                        "call_id": call.call_id,
                        "canonical_name": call.canonical_name,
                        "raw_name": call.raw_name,
                        "round": 1,
                        "command": "delete obsolete-http.txt",
                    },
                    caused_by=call.call_id,
                )
            )
            waiting = await execute_normalized_tool_call(call, execution_context)
            approval_id = waiting.data["approval"]["approval_id"]
            approval_capture.update(
                approval_id=approval_id,
                call_id=call.call_id,
            )
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "tool_result",
                    {"result": waiting.as_dict(), "round": 1},
                    caused_by=call.call_id,
                )
            )
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "run_state",
                    {
                        "state": RunState.WAITING_APPROVAL.value,
                        "reason": "waiting_for_exact_effect_approval",
                        "resumable": True,
                        "terminal": False,
                        "approval_id": approval_id,
                        "call_id": call.call_id,
                    },
                    caused_by=call.call_id,
                )
            )
            approval_ready.set()
            decision = await EFFECT_APPROVALS.wait(
                approval_id,
                context=execution_context,
            )
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "run_state",
                    {
                        "state": RunState.RUNNING.value,
                        "reason": f"effect_approval_{decision.value}",
                        "terminal": False,
                    },
                    caused_by=call.call_id,
                )
            )
            resumed = await execute_normalized_tool_call(
                call,
                execution_context,
                approval_id=approval_id,
            )
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "tool_resumed",
                    {
                        "call_id": call.call_id,
                        "canonical_name": call.canonical_name,
                        "approval_id": approval_id,
                        "round": 1,
                    },
                    caused_by=call.call_id,
                )
            )
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "tool_result",
                    {
                        "result": resumed.as_dict(),
                        "round": 1,
                        "approval_id": approval_id,
                    },
                    caused_by=call.call_id,
                )
            )
            succeeded = resumed.status.value == "success"
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "run_state",
                    {
                        "state": (
                            RunState.COMPLETED.value
                            if succeeded
                            else RunState.FAILED.value
                        ),
                        "disposition": "completed" if succeeded else "error",
                        "reason": "approved_effect_committed",
                        "resumable": False,
                        "terminal": True,
                    },
                )
            )
        elif "sudo id" in latest:
            call = normalize_tool_calls(
                [ToolBlock("run_host_command", "", arguments={"command": "sudo id"})],
                [],
                execution_context=execution_context,
                provider_name="http-sse-test",
            )[0]
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "tool_started",
                    {
                        "call_id": call.call_id,
                        "canonical_name": call.canonical_name,
                        "raw_name": call.raw_name,
                        "round": 1,
                        "command": "sudo id",
                    },
                    caused_by=call.call_id,
                )
            )
            result = await execute_normalized_tool_call(call, execution_context)
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "tool_result",
                    {"result": result.as_dict(), "round": 1, "command": "sudo id"},
                    caused_by=call.call_id,
                )
            )
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "run_state",
                    {
                        "state": RunState.WAITING_APPROVAL.value,
                        "disposition": "awaiting_approval",
                        "reason": "sensitive_effect",
                        "resumable": True,
                        "terminal": False,
                    },
                )
            )
        else:
            yield encode_runtime_sse(
                execution_context.event_factory.create(
                    "run_state",
                    {
                        "state": RunState.COMPLETED.value,
                        "disposition": "completed",
                        "reason": "http_test_complete",
                        "resumable": False,
                        "terminal": True,
                    },
                )
            )
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes, "stream_agent_loop", agent_stream)

    def start(session_id, source, **kwargs):
        execution_context = kwargs["execution_context"]
        prepared_turn = kwargs["prepared_turn"]
        from src.agent.runtime_v2.authority import activate_execution_context

        before_commit_count = len(session_manager.session.history)
        activate_execution_context(execution_context, prepared_turn.lease)
        kwargs["commit_callback"]()
        assert len(session_manager.session.history) == before_commit_count + 1
        captured_contexts.append(execution_context)
        buffered = []

        async def drain():
            async for event in source:
                buffered.append(event)

        task = asyncio.create_task(drain())
        runs[session_id] = (task, buffered)
        return SimpleNamespace(task=task)

    async def subscribe(session_id):
        task, buffered = runs[session_id]
        await task
        for event in buffered:
            yield event

    monkeypatch.setattr(chat_routes.agent_runs, "start", start)
    monkeypatch.setattr(chat_routes.agent_runs, "subscribe", subscribe)

    app = FastAPI()
    app.include_router(
        chat_routes.setup_chat_routes(
            session_manager,
            chat_handler=None,
            chat_processor=None,
            memory_manager=None,
            research_handler=None,
            upload_handler=None,
        )
    )
    client = TestClient(app)

    common = {
        "message": "Run pwd and report the root.",
        "session": "runtime-v2-http",
        "mode": "agent",
        "allow_bash": "true",
    }

    selected_response = client.post(
        "/api/chat_stream",
        data={**common, "shell_mode": "sandboxed", "workspace": str(tmp_path)},
    )
    assert selected_response.status_code == 200
    selected = captured_contexts[-1]
    assert selected.execution_mode is ExecutionMode.SANDBOXED
    assert selected.execution_root.path == str(tmp_path.resolve())
    assert selected.execution_root.source.value == "selected_workspace"
    selected_result = _execute(selected, "bash", {"command": "pwd"})
    assert selected_result.data["execution_root"] == str(tmp_path.resolve())
    assert selected_result.data["stdout"].strip() == str(tmp_path.resolve())

    for index in range(5):
        (tmp_path / f"search-{index}.txt").write_text(
            "http-needle\n" if index == 4 else "no match\n",
            encoding="utf-8",
        )
    search_response = client.post(
        "/api/chat_stream",
        data={
            **common,
            "message": "Search continuation over the disposable workspace.",
            "shell_mode": "sandboxed",
            "workspace": str(tmp_path),
        },
    )
    assert search_response.status_code == 200
    search_events = [
        json.loads(line[6:])
        for line in search_response.text.splitlines()
        if line.startswith("data: {")
    ]
    search_results = [
        event["payload"]["result"]
        for event in search_events
        if event.get("type") == "tool_result"
    ]
    assert any(
        not result["data"]["matches"] and result["continuation"]
        for result in search_results
    )
    assert [
        match["path"]
        for result in search_results
        for match in result["data"]["matches"]
    ] == ["search-4.txt"]
    assert search_results[-1]["continuation"] is None

    no_workspace_response = client.post(
        "/api/chat_stream",
        data={**common, "shell_mode": "sandboxed"},
    )
    assert no_workspace_response.status_code == 200
    no_workspace = captured_contexts[-1]
    assert no_workspace.execution_mode is ExecutionMode.SANDBOXED
    assert no_workspace.execution_root.source.value == "ephemeral_workspace"
    assert no_workspace.execution_root.path != str(tmp_path.resolve())
    ephemeral_result = _execute(no_workspace, "bash", {"command": "pwd"})
    assert ephemeral_result.data["stdout"].strip() == no_workspace.execution_root.path

    forged = client.post(
        "/api/chat_stream",
        data={**common, "shell_mode": "host", "workspace": str(tmp_path)},
    )
    assert forged.status_code == 200
    assert captured_contexts[-1].execution_mode is ExecutionMode.DISABLED

    authorization = client.post(
        "/api/chat/host-authorize",
        json={"session_id": "runtime-v2-http"},
    )
    assert authorization.status_code == 200
    token = authorization.json()["authorization"]

    authorized = client.post(
        "/api/chat_stream",
        data={
            **common,
            "shell_mode": "host",
            "workspace": str(tmp_path),
            "host_authorization": token,
        },
    )
    assert authorized.status_code == 200
    host_context = captured_contexts[-1]
    assert host_context.execution_mode is ExecutionMode.HOST
    assert host_context.execution_root.path == str(tmp_path.resolve())
    host_result = _execute_approved(
        host_context,
        "run_host_command",
        {"command": "pwd"},
    )
    assert host_result.data["stdout"].strip() == str(tmp_path.resolve())

    approval_authorization = client.post(
        "/api/chat/host-authorize",
        json={"session_id": "runtime-v2-http"},
    )
    approval_token = approval_authorization.json()["authorization"]
    approval = client.post(
        "/api/chat_stream",
        data={
            **common,
            "message": "Run sudo id",
            "shell_mode": "host",
            "workspace": str(tmp_path),
            "host_authorization": approval_token,
        },
    )
    assert approval.status_code == 200
    assert '"type":"tool_started"' in approval.text
    assert '"type":"tool_result"' in approval.text
    assert '"status":"approval_required"' in approval.text
    assert '"state":"waiting_approval"' in approval.text

    target = tmp_path / "obsolete-http.txt"
    target.write_text("delete after exact approval\n", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending_response = pool.submit(
            client.post,
            "/api/chat_stream",
            data={
                **common,
                "message": "Delete obsolete-http.txt",
                "shell_mode": "disabled",
                "workspace": str(tmp_path),
                "allow_workspace_write": "true",
            },
        )
        assert approval_ready.wait(timeout=5)
        exact_approval_id = approval_capture["approval_id"]
        decision = TestClient(app).post(
            f"/api/chat/approvals/{exact_approval_id}",
            json={"decision": "allow"},
        )
        continued = pending_response.result(timeout=10)

    assert decision.status_code == 200
    assert decision.json()["state"] == "granted"
    assert continued.status_code == 200
    assert '"type":"tool_resumed"' in continued.text
    assert '"reason":"approved_effect_committed"' in continued.text
    assert not target.exists()
    assert EFFECT_APPROVALS.state(exact_approval_id).value == "committed"
    replayed_decision = client.post(
        f"/api/chat/approvals/{exact_approval_id}",
        json={"decision": "allow"},
    )
    assert replayed_decision.status_code == 409

    replay = client.post(
        "/api/chat_stream",
        data={
            **common,
            "shell_mode": "host",
            "workspace": str(tmp_path),
            "host_authorization": token,
        },
    )
    assert replay.status_code == 200
    assert captured_contexts[-1].execution_mode is ExecutionMode.DISABLED

    principal.update(name="public-user", admin=False)
    denied = client.post(
        "/api/chat/host-authorize",
        json={"session_id": "runtime-v2-http"},
    )
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_invalid_http_replacement_preserves_existing_detached_run(monkeypatch):
    session_manager = _SessionManager()
    session_manager.session.model = ""
    release = asyncio.Event()

    async def existing_source():
        await release.wait()
        yield "data: [DONE]\n\n"

    existing = chat_routes.agent_runs.start(
        "runtime-v2-http",
        existing_source(),
        mode=chat_routes.agent_runs.RunMode.DETACHED,
        owner="admin",
    )
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_routes, "get_current_user", lambda request: "admin")
    monkeypatch.setattr(chat_routes, "effective_user", lambda request: "admin")
    monkeypatch.setattr(
        chat_routes,
        "_reconcile_selected_route_from_request",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        chat_routes,
        "_clear_orphaned_session_endpoint",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        chat_routes,
        "_recover_empty_session_model",
        lambda *args, **kwargs: False,
    )

    app = FastAPI()
    app.include_router(
        chat_routes.setup_chat_routes(
            session_manager,
            chat_handler=None,
            chat_processor=None,
            memory_manager=None,
            research_handler=None,
            upload_handler=None,
        )
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://runtime-v2.test",
    ) as client:
        response = await client.post(
            "/api/chat_stream",
            data={
                "message": "This malformed replacement has no model.",
                "session": "runtime-v2-http",
                "mode": "agent",
            },
        )

    assert response.status_code == 400
    assert "No model selected" in response.text
    assert not existing.task.done()
    assert chat_routes.agent_runs.is_active("runtime-v2-http")

    release.set()
    await existing.task
