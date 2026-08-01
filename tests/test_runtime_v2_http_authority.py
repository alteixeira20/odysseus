"""HTTP-backed authority preparation for the production chat routes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.chat_routes as chat_routes
from routes.chat_helpers import ChatContext, PreprocessedMessage, PresetInfo
from src.agent.rounds.tool_calls import normalize_tool_calls
from src.agent.runtime_v2.executor import execute_normalized_tool_call
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


def _execute(context, name, arguments):
    call = normalize_tool_calls(
        [ToolBlock(name, "", arguments=arguments)],
        [],
        execution_context=context,
        provider_name="http-authority-test",
    )[0]
    return asyncio.run(execute_normalized_tool_call(call, context))


def test_production_http_preparation_binds_roots_and_one_run_host_authority(
    monkeypatch,
    tmp_path,
):
    session_manager = _SessionManager()
    principal = {"name": "admin", "admin": True}
    captured_contexts = []
    runs = {}

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
        if "sudo id" in latest:
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
                        "terminal": True,
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
        captured_contexts.append(kwargs["execution_context"])
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
    host_result = _execute(host_context, "run_host_command", {"command": "pwd"})
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
