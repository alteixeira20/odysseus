"""Authoritative runtime event, terminal-state, and reconnect contracts."""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import agent_runs
import routes.chat_routes as chat_routes
from src.agent.runtime_v2.authority import prepare_execution_context
from src.agent.runtime_v2.contracts import RunBudgets
from src.agent.runtime_v2.events import encode_runtime_sse, runtime_event_from_payload
from src.agent.runtime_v2.state import RunState
from src.agent.tools.bootstrap import TOOL_REGISTRY


def context(tmp_path, session):
    prepared, _ = prepare_execution_context(
        owner_id="owner",
        session_id=session,
        requested_mode="disabled",
        selected_workspace=str(tmp_path),
        budgets=RunBudgets(wall_clock_seconds=5, idle_seconds=2),
        tool_catalog_revision=TOOL_REGISTRY.revision,
    )
    return prepared


def runtime_state_event(prepared, state, *, disposition=None, terminal=False, reason=None):
    payload = {
        "state": state,
        "reason": reason or state,
        "terminal": terminal,
    }
    if disposition is not None:
        payload["disposition"] = disposition
    return encode_runtime_sse(
        prepared.event_factory.create("run_state", payload)
    )


def parsed_runtime_events(events):
    parsed = []
    for wire in events:
        if not wire.startswith("data: {"):
            continue
        payload = json.loads(wire[6:])
        event = runtime_event_from_payload(payload)
        if event is not None:
            parsed.append(event)
    return parsed


@pytest.mark.asyncio
async def test_approval_waiting_is_terminal_but_never_completed(tmp_path):
    session = "runtime-v2-approval"
    prepared = context(tmp_path, session)

    async def producer():
        yield runtime_state_event(prepared, "preparing")
        yield runtime_state_event(prepared, "running")
        yield runtime_state_event(
            prepared,
            "waiting_approval",
            disposition="awaiting_approval",
            terminal=True,
            reason="sensitive_effect",
        )
        yield "data: [DONE]\n\n"

    run = agent_runs.start(
        session,
        producer(),
        mode=agent_runs.RunMode.DETACHED,
        owner="owner",
        execution_context=prepared,
    )
    events = [event async for event in agent_runs.subscribe(session)]
    await run.task
    assert agent_runs.get_status(session) == "awaiting_approval"
    assert run.state_machine.state is RunState.WAITING_APPROVAL
    assert run.terminal.disposition.value == "awaiting_approval"
    assert events[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_done_without_semantic_terminal_becomes_incomplete(tmp_path):
    session = "runtime-v2-done-only"
    prepared = context(tmp_path, session)

    async def producer():
        yield "data: [DONE]\n\n"

    run = agent_runs.start(
        session,
        producer(),
        execution_context=prepared,
        owner="owner",
    )
    events = [event async for event in agent_runs.subscribe(session)]
    await run.task
    assert agent_runs.get_status(session) == "incomplete"
    assert run.state_machine.state is RunState.INCOMPLETE
    typed = parsed_runtime_events(events)
    assert typed[-1].payload["state"] == "incomplete"
    assert typed[-1].payload["reason"] == "done_without_semantic_terminal"


@pytest.mark.asyncio
async def test_stream_error_overrides_pending_success_in_typed_and_internal_state(tmp_path):
    session = "runtime-v2-error-override"
    prepared = context(tmp_path, session)

    async def producer():
        yield runtime_state_event(prepared, "preparing")
        yield runtime_state_event(prepared, "running")
        yield runtime_state_event(
            prepared,
            "completed",
            disposition="completed",
            terminal=True,
        )
        yield 'event: error\ndata: {"error":"provider failed","status":500}\n\n'
        yield "data: [DONE]\n\n"

    run = agent_runs.start(
        session,
        producer(),
        execution_context=prepared,
        owner="owner",
    )
    events = [event async for event in agent_runs.subscribe(session)]
    await run.task
    typed = parsed_runtime_events(events)
    assert run.terminal.disposition.value == "error"
    assert run.state_machine.state is RunState.FAILED
    assert typed[-1].payload["state"] == "failed"
    assert not any(event.payload["state"] == "completed" for event in typed)


@pytest.mark.asyncio
async def test_detached_subscriber_disconnect_does_not_cancel_run(tmp_path):
    session = "runtime-v2-detached"
    prepared = context(tmp_path, session)
    release = asyncio.Event()

    async def producer():
        yield runtime_state_event(prepared, "preparing")
        yield runtime_state_event(prepared, "running")
        await release.wait()
        yield runtime_state_event(
            prepared,
            "completed",
            disposition="completed",
            terminal=True,
        )
        yield "data: [DONE]\n\n"

    run = agent_runs.start(
        session,
        producer(),
        mode=agent_runs.RunMode.DETACHED,
        owner="owner",
        execution_context=prepared,
    )
    subscriber = agent_runs.subscribe(session)
    assert (await anext(subscriber)).startswith("data: {")
    await subscriber.aclose()
    assert not prepared.cancellation_token.cancelled
    assert not run.task.done()

    release.set()
    replay = [event async for event in agent_runs.subscribe(session)]
    await run.task
    sequences = [event.sequence for event in parsed_runtime_events(replay)]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert run.state_machine.state is RunState.COMPLETED


def test_http_resume_replays_a_retained_terminal_run(monkeypatch):
    monkeypatch.setattr(chat_routes, "_verify_session_owner", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_routes.agent_runs, "get_status", lambda session_id: "done")

    async def subscribe(session_id):
        yield 'data: {"type":"run_state","state":"completed","terminal":true}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chat_routes.agent_runs, "subscribe", subscribe)
    app = FastAPI()
    app.include_router(chat_routes.setup_chat_routes(None, None, None, None, None, None))

    response = TestClient(app).get("/api/chat/resume/retained-run")

    assert response.status_code == 200
    assert '"state":"completed"' in response.text
    assert response.text.endswith("data: [DONE]\n\n")
