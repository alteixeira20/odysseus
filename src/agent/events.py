"""Typed agent events and the existing frontend SSE wire encoder."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from typing import Any, Callable, Iterator, Mapping, Optional, Union

from src.agent.contracts import RunDisposition


@dataclass(frozen=True)
class AgentEvent:
    """A structured event before projection onto the legacy SSE contract."""

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    sse_event: Optional[str] = None

    @classmethod
    def delta(cls, text: str) -> "AgentEvent":
        return cls("delta", {"delta": text})

    @classmethod
    def typed(cls, event_type: str, **payload: Any) -> "AgentEvent":
        return cls(event_type, {"type": event_type, **payload})

    @classmethod
    def error(cls, **payload: Any) -> "AgentEvent":
        return cls("error", payload, sse_event="error")

    @classmethod
    def done(cls) -> "AgentEvent":
        return cls("done")


AgentEventObserver = Callable[[AgentEvent, str], None]
_AGENT_EVENT_OBSERVERS: ContextVar[tuple[AgentEventObserver, ...]] = ContextVar(
    "agent_event_observers",
    default=(),
)


@contextmanager
def observe_agent_events(observer: AgentEventObserver) -> Iterator[None]:
    """Observe typed events encoded in the current async/task context.

    The observer receives the immutable ``AgentEvent`` and its exact legacy SSE
    projection.  ContextVars keep concurrent Agent runs isolated while nested
    observers compose rather than replacing one another.  Observer failures are
    deliberately not swallowed: an authoritative lifecycle observer must fail
    the run rather than silently losing terminal semantics.
    """

    current = _AGENT_EVENT_OBSERVERS.get()
    token = _AGENT_EVENT_OBSERVERS.set((*current, observer))
    try:
        yield
    finally:
        _AGENT_EVENT_OBSERVERS.reset(token)


def _notify_observers(event: AgentEvent, wire: str) -> None:
    for observer in _AGENT_EVENT_OBSERVERS.get():
        observer(event, wire)


def encode_legacy_sse(event: AgentEvent) -> str:
    """Encode exactly the SSE shape consumed by the current chat frontend."""

    if event.kind == "done":
        wire = "data: [DONE]\n\n"
    else:
        prefix = f"event: {event.sse_event}\n" if event.sse_event else ""
        wire = f"{prefix}data: {json.dumps(dict(event.payload))}\n\n"
    _notify_observers(event, wire)
    return wire


def run_status_event(phase: str, label: str, **extra: Any) -> str:
    """Encode the existing ephemeral agent-status event."""

    return encode_legacy_sse(
        AgentEvent.typed(
            "run_status",
            phase=phase,
            label=label,
            ephemeral=True,
            **extra,
        )
    )


def run_state_event(
    state: Union[str, RunDisposition], **extra: Any
) -> str:
    """Encode the single semantic terminal lifecycle state for a run."""

    state_value = state.value if isinstance(state, RunDisposition) else str(state)

    return encode_legacy_sse(
        AgentEvent.typed(
            "run_state",
            state=state_value,
            terminal=True,
            **extra,
        )
    )


__all__ = [
    "AgentEvent",
    "AgentEventObserver",
    "encode_legacy_sse",
    "observe_agent_events",
    "run_state_event",
    "run_status_event",
]
