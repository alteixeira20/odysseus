import asyncio

from src.agent.contracts import RunDisposition
from src.agent.events import (
    AgentEvent,
    encode_legacy_sse,
    observe_agent_events,
    run_state_event,
)


def test_observer_receives_typed_event_and_exact_wire_without_changing_output():
    seen = []
    event = AgentEvent.typed(
        "run_state",
        state="completed",
        terminal=True,
        reason="done",
    )

    with observe_agent_events(lambda typed, wire: seen.append((typed, wire))):
        wire = encode_legacy_sse(event)

    assert seen == [(event, wire)]
    assert wire == (
        'data: {"type": "run_state", "state": "completed", '
        '"terminal": true, "reason": "done"}\n\n'
    )


def test_nested_observers_compose_and_restore():
    outer = []
    inner = []

    with observe_agent_events(lambda event, wire: outer.append(event.kind)):
        encode_legacy_sse(AgentEvent.delta("outer-1"))
        with observe_agent_events(lambda event, wire: inner.append(event.kind)):
            encode_legacy_sse(AgentEvent.delta("both"))
        encode_legacy_sse(AgentEvent.delta("outer-2"))

    encode_legacy_sse(AgentEvent.delta("unobserved"))
    assert outer == ["delta", "delta", "delta"]
    assert inner == ["delta"]


async def _capture_terminal(label: str):
    seen = []
    with observe_agent_events(
        lambda event, wire: seen.append((event.payload.get("reason"), wire))
    ):
        await asyncio.sleep(0)
        wire = run_state_event(
            RunDisposition.COMPLETED,
            reason=label,
        )
        await asyncio.sleep(0)
    return wire, seen


def test_contextvar_observers_do_not_leak_between_concurrent_runs():
    async def run():
        return await asyncio.gather(
            _capture_terminal("run-a"),
            _capture_terminal("run-b"),
        )

    (wire_a, seen_a), (wire_b, seen_b) = asyncio.run(run())

    assert seen_a == [("run-a", wire_a)]
    assert seen_b == [("run-b", wire_b)]
