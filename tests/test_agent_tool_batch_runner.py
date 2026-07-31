import json
from types import SimpleNamespace

import pytest

from src.agent.execution.batch_runner import (
    BatchDisposition,
    ToolBatchRequest,
    ToolBatchRunner,
    ToolBatchState,
)
from src.agent.execution.observation_ledger import ObservationLedger


def _block(name="bash", content='{"command":"printf ok"}'):
    return SimpleNamespace(tool_type=name, content=content)


def _request(blocks, **overrides):
    values = {
        "tool_blocks": blocks,
        "converted_calls": [],
        "used_native": False,
        "round_response": "calling",
        "round_reasoning": "",
        "round_number": 1,
        "max_tool_calls": 0,
        "session_id": "session",
        "owner": "owner",
        "workspace": "/workspace",
        "disabled_tools": set(),
        "allowed_tools": {"bash", "ask_user"},
    }
    values.update(overrides)
    return ToolBatchRequest(**values)


def _state(total=0):
    return ToolBatchState(
        messages=[],
        full_response="",
        total_tool_calls=total,
        tool_events=[],
        relevant_tools=set(),
    )


def _events(chunks):
    return [
        json.loads(chunk[6:])
        for chunk in chunks
        if chunk.startswith("data: ")
    ]


def _runner(request, state, execute, appended, *, format_result=None):
    def append_results(*args, **kwargs):
        appended.append((args, kwargs))

    return ToolBatchRunner(
        request,
        state,
        execute_tool=execute,
        format_result=format_result or (
            lambda desc, result: (
                f"{desc}: {result.get('output', result.get('error', ''))}"
            )
        ),
        append_results=append_results,
        strip_tool_blocks=lambda text, **kwargs: text,
        bash_timeout_for_block=lambda block: (
            120.0 if block.tool_type == "bash" else None
        ),
        effectful_tools={"bash"},
        invocation_id=lambda size: "nonce",
    )


@pytest.mark.asyncio
async def test_allowed_tool_preserves_start_progress_output_step_order():
    async def execute(block, **kwargs):
        await kwargs["progress_cb"]({"elapsed_s": 1, "tail": "ok"})
        return "bash", {"output": "ok", "exit_code": 0}

    state = _state()
    appended = []
    runner = _runner(_request([_block()]), state, execute, appended)

    chunks = [chunk async for chunk in runner.stream()]
    events = _events(chunks)

    assert [event.get("type") for event in events] == [
        "run_status",
        "tool_start",
        "tool_progress",
        "tool_output",
        "agent_step",
    ]
    assert events[1]["invocation_id"] == "nonce"
    assert events[2]["invocation_id"] == "nonce"
    assert events[3]["invocation_id"] == "nonce"
    assert state.total_tool_calls == 1
    assert state.effectful_used is True
    assert len(state.tool_events) == 1
    assert len(appended) == 1
    assert runner.outcome is not None
    assert runner.outcome.disposition is BatchDisposition.CONTINUE


@pytest.mark.asyncio
async def test_policy_block_consumes_budget_without_start_or_progress():
    class Policy:
        def blocks(self, name):
            return True

        def reason_for(self, name):
            return "disabled"

    async def execute(*args, **kwargs):
        raise AssertionError("blocked tool must not execute")

    state = _state()
    appended = []
    runner = _runner(
        _request([_block()], tool_policy=Policy()),
        state,
        execute,
        appended,
    )

    events = _events([chunk async for chunk in runner.stream()])

    assert [event.get("type") for event in events] == [
        "tool_output",
        "agent_step",
    ]
    assert events[0]["exit_code"] == 1
    assert state.total_tool_calls == 1
    assert len(appended) == 1


@pytest.mark.asyncio
async def test_budget_hit_before_second_tool_does_not_thread_partial_batch():
    async def execute(block, **kwargs):
        return block.tool_type, {"output": "ok", "exit_code": 0}

    state = _state()
    appended = []
    runner = _runner(
        _request([_block(), _block()], max_tool_calls=1),
        state,
        execute,
        appended,
    )

    events = _events([chunk async for chunk in runner.stream()])

    assert [event.get("type") for event in events][-1] == (
        "budget_exceeded"
    )
    assert state.total_tool_calls == 1
    assert appended == []
    assert runner.outcome is not None
    assert (
        runner.outcome.disposition
        is BatchDisposition.BUDGET_EXHAUSTED
    )


@pytest.mark.asyncio
async def test_ask_user_persists_question_and_stops_before_threading():
    async def execute(block, **kwargs):
        return "ask_user", {
            "ask_user": {
                "question": "Which option?",
                "options": ["A", "B"],
            },
            "output": "waiting",
            "exit_code": 0,
        }

    state = _state()
    appended = []
    runner = _runner(
        _request([_block("ask_user", "{}")]),
        state,
        execute,
        appended,
    )

    events = _events([chunk async for chunk in runner.stream()])

    assert [event.get("type", "delta") for event in events] == [
        "run_status",
        "tool_start",
        "delta",
        "tool_output",
        "ask_user",
    ]
    assert state.full_response == "Which option?"
    assert state.tool_events[0]["ask_user"]["question"] == (
        "Which option?"
    )
    assert appended == []
    assert runner.outcome is not None
    assert runner.outcome.disposition is BatchDisposition.AWAIT_USER


@pytest.mark.asyncio
async def test_repeated_read_is_marked_in_live_batch_without_hiding_output():
    from src.tool_execution import format_tool_result

    async def execute(block, **kwargs):
        return "read_file: sample.py", {"output": "important bytes", "exit_code": 0}

    ledger = ObservationLedger()
    state = _state()
    appended = []
    request = _request(
        [
            _block("read_file", '{"path":"sample.py"}'),
            _block("read_file", '{"path":"sample.py"}'),
        ],
        observation_ledger=ledger,
        allowed_tools={"read_file"},
    )
    runner = _runner(
        request,
        state,
        execute,
        appended,
        format_result=format_tool_result,
    )

    chunks = [chunk async for chunk in runner.stream()]

    assert len(appended) == 1
    threaded_results = appended[0][0][3]
    assert "important bytes" in threaded_results[0]
    assert "important bytes" in threaded_results[1]
    assert "Repeated observation" in threaded_results[1]
    assert len(ledger) == 1
    assert len([event for event in _events(chunks) if event.get("type") == "tool_output"]) == 2
