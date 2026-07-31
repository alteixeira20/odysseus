"""Typed agent events and the existing frontend SSE wire encoder."""

from dataclasses import dataclass, field
import json
from typing import Any, Mapping, Optional


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


def encode_legacy_sse(event: AgentEvent) -> str:
    """Encode exactly the SSE shape consumed by the current chat frontend."""

    if event.kind == "done":
        return "data: [DONE]\n\n"
    prefix = f"event: {event.sse_event}\n" if event.sse_event else ""
    return f"{prefix}data: {json.dumps(dict(event.payload))}\n\n"


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
