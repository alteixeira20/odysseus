"""End-to-end completion, continuation, and terminal-state contracts."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from src import agent_loop, agent_runs


def _patch_runtime(monkeypatch):
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(
        agent_loop,
        "_build_system_prompt",
        lambda messages, *args, **kwargs: (
            [{"role": "system", "content": "SYSTEM"}] + list(messages),
            [],
        ),
    )


def _events(chunks):
    return [
        json.loads(chunk[6:])
        for chunk in chunks
        if chunk.startswith("data: {")
    ]


async def _run(monkeypatch, provider, **kwargs):
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    return [
        chunk
        async for chunk in agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "Inspect and complete the task."}],
            relevant_tools={"bash", "read_file"},
            owner="admin",
            max_rounds=8,
            _is_teacher_run=True,
            **kwargs,
        )
    ]


def _terminal_events(chunks):
    return [
        event
        for event in _events(chunks)
        if event.get("type") == "run_state" and event.get("terminal")
    ]


@pytest.mark.asyncio
async def test_ordinary_completion_has_exactly_one_terminal_state_before_done(monkeypatch):
    async def provider(*args, **kwargs):
        yield 'data: {"delta": "Complete."}\n\n'
        yield 'data: {"type": "finish", "reason": "stop"}\n\n'
        yield "data: [DONE]\n\n"

    chunks = await _run(monkeypatch, provider)

    assert _terminal_events(chunks) == [
        {
            "type": "run_state",
            "state": "completed",
            "terminal": True,
            "reason": "completed",
            "resumable": False,
        }
    ]
    assert chunks[-2].startswith('data: {"type": "run_state"')
    assert chunks[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_clean_protocol_done_without_finish_reason_does_not_continue(monkeypatch):
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield 'data: {"delta": "Protocol-complete answer."}\n\n'
        yield "data: [DONE]\n\n"

    chunks = await _run(monkeypatch, provider)

    assert calls == 1
    assert not [e for e in _events(chunks) if e.get("type") == "truncation_continuation"]
    assert len(_terminal_events(chunks)) == 1


@pytest.mark.asyncio
async def test_unexpected_eof_after_partial_text_continues(monkeypatch):
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield 'data: {"delta": "Partial before EOF"}\n\n'
            yield 'data: {"type": "finish", "reason": null, "protocol_terminal_seen": false}\n\n'
            return
        yield 'data: {"delta": " and recovered."}\n\n'
        yield 'data: {"type": "finish", "reason": "stop"}\n\n'
        yield "data: [DONE]\n\n"

    chunks = await _run(monkeypatch, provider)
    continuations = [e for e in _events(chunks) if e.get("type") == "truncation_continuation"]

    assert calls == 2
    assert len(continuations) == 1
    assert continuations[0]["termination_kind"] == "transport_eof"
    assert len(_terminal_events(chunks)) == 1


@pytest.mark.asyncio
async def test_incomplete_native_call_continues_without_dispatch(monkeypatch):
    calls = 0
    execute = AsyncMock()

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [{"id": "partial", "name": "read_file", "arguments": '{"path":'}],
            }) + "\n\n"
            yield 'data: {"type": "finish", "reason": "stop"}\n\n'
            yield "data: [DONE]\n\n"
            return
        yield 'data: {"delta": "Recovered without dispatching the fragment."}\n\n'
        yield 'data: {"type": "finish", "reason": "stop"}\n\n'
        yield "data: [DONE]\n\n"

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)
    chunks = await _run(monkeypatch, provider)

    assert calls == 2
    execute.assert_not_awaited()
    assert len([e for e in _events(chunks) if e.get("type") == "truncation_continuation"]) == 1


@pytest.mark.asyncio
async def test_incomplete_fenced_call_continues_without_dispatch(monkeypatch):
    calls = 0
    execute = AsyncMock()

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield 'data: {"delta": "```bash\\necho unfinished"}\n\n'
            yield 'data: {"type": "finish", "reason": "stop"}\n\n'
            yield "data: [DONE]\n\n"
            return
        yield 'data: {"delta": "Recovered."}\n\n'
        yield 'data: {"type": "finish", "reason": "stop"}\n\n'
        yield "data: [DONE]\n\n"

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)
    chunks = [
        chunk
        async for chunk in agent_loop.stream_agent_loop(
            "http://local.invalid/v1",
            "plain-model",
            [{"role": "user", "content": "Run the check."}],
            relevant_tools={"bash"},
            owner="admin",
            max_rounds=3,
            _is_teacher_run=True,
        )
    ]

    assert calls == 2
    execute.assert_not_awaited()
    assert len([e for e in _events(chunks) if e.get("type") == "truncation_continuation"]) == 1


@pytest.mark.asyncio
async def test_complete_tool_from_interrupted_round_executes_exactly_once(monkeypatch):
    provider_round = 0
    execute = AsyncMock(return_value=("bash: pwd", {"output": "/work", "exit_code": 0}))

    async def provider(*args, **kwargs):
        nonlocal provider_round
        provider_round += 1
        if provider_round == 1:
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [{"id": "one", "name": "bash", "arguments": '{"command":"pwd"}'}],
            }) + "\n\n"
            yield 'data: {"type": "finish", "reason": null, "protocol_terminal_seen": false}\n\n'
            return
        yield 'data: {"delta": "Verified."}\n\n'
        yield 'data: {"type": "finish", "reason": "stop"}\n\n'
        yield "data: [DONE]\n\n"

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)
    chunks = await _run(monkeypatch, provider)

    assert provider_round == 2
    execute.assert_awaited_once()
    assert not [e for e in _events(chunks) if e.get("type") == "truncation_continuation"]


@pytest.mark.asyncio
async def test_announcement_only_round_is_nudged_then_executes(monkeypatch):
    provider_round = 0
    execute = AsyncMock(return_value=("bash: pwd", {"output": "/work", "exit_code": 0}))

    async def provider(*args, **kwargs):
        nonlocal provider_round
        provider_round += 1
        if provider_round == 1:
            yield 'data: {"delta": "I will inspect the repository now"}\n\n'
        elif provider_round == 2:
            yield "data: " + json.dumps({
                "type": "tool_calls",
                "calls": [{"id": "one", "name": "bash", "arguments": '{"command":"pwd"}'}],
            }) + "\n\n"
        else:
            yield 'data: {"delta": "Inspection complete."}\n\n'
        yield 'data: {"type": "finish", "reason": "stop"}\n\n'
        yield "data: [DONE]\n\n"

    _patch_runtime(monkeypatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)
    chunks = await _run(monkeypatch, provider)

    assert provider_round == 3
    execute.assert_awaited_once()
    assert len(_terminal_events(chunks)) == 1


@pytest.mark.asyncio
async def test_detached_run_marks_generator_close_without_done_incomplete(monkeypatch):
    monkeypatch.setattr(agent_runs, "_RUNS", {})

    async def producer():
        yield 'data: {"delta": "answer"}\n\n'
        # A legacy producer closes without terminal metadata or [DONE].

    agent_runs.start("terminal-contract", producer())
    chunks = [chunk async for chunk in agent_runs.subscribe("terminal-contract")]

    assert len(_terminal_events(chunks)) == 1
    assert _terminal_events(chunks)[0]["state"] == "incomplete"
    assert _terminal_events(chunks)[0]["reason"] == "generator_closed_without_done"
    assert _terminal_events(chunks)[0]["resumable"] is True
    assert chunks[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_detached_run_exception_has_one_error_terminal_and_done(monkeypatch):
    monkeypatch.setattr(agent_runs, "_RUNS", {})

    async def producer():
        yield 'data: {"delta": "partial"}\n\n'
        raise RuntimeError("transport exploded")

    agent_runs.start("terminal-error-contract", producer())
    chunks = [chunk async for chunk in agent_runs.subscribe("terminal-error-contract")]

    terminals = _terminal_events(chunks)
    assert len(terminals) == 1
    assert terminals[0]["state"] == "error"
    assert chunks[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_explicit_error_terminal_controls_internal_status_after_done(monkeypatch):
    monkeypatch.setattr(agent_runs, "_RUNS", {})

    async def producer():
        yield 'data: {"type":"run_state","state":"error","reason":"provider_error","terminal":true}\n\n'
        yield "data: [DONE]\n\n"

    run = agent_runs.start("error-status-contract", producer())
    chunks = [chunk async for chunk in agent_runs.subscribe("error-status-contract")]

    assert run.status == "error"
    assert [event["state"] for event in _terminal_events(chunks)] == ["error"]


@pytest.mark.asyncio
async def test_exception_overrides_uncommitted_success_terminal(monkeypatch):
    monkeypatch.setattr(agent_runs, "_RUNS", {})

    async def producer():
        yield 'data: {"type":"run_state","state":"completed","reason":"premature","terminal":true}\n\n'
        raise RuntimeError("failed before protocol completion")

    run = agent_runs.start("premature-success-contract", producer())
    chunks = [chunk async for chunk in agent_runs.subscribe("premature-success-contract")]

    assert run.status == "error"
    assert [event["state"] for event in _terminal_events(chunks)] == ["error"]
