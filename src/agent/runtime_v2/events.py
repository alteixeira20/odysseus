"""Versioned, ordered Runtime V2 events and their SSE projection."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Optional


@dataclass(frozen=True)
class RuntimeEvent:
    version: int
    event_id: str
    run_id: str
    conversation_id: str
    turn_id: str
    candidate_id: Optional[str]
    sequence: int
    timestamp: str
    type: str
    caused_by: Optional[str]
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "candidate_id": self.candidate_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "type": self.type,
            "caused_by": self.caused_by,
            "payload": dict(self.payload),
        }


RuntimeEventObserver = Callable[[RuntimeEvent, str], None]
_RUNTIME_EVENT_OBSERVERS: ContextVar[tuple[RuntimeEventObserver, ...]] = ContextVar(
    "runtime_v2_event_observers",
    default=(),
)


@contextmanager
def observe_runtime_events(observer: RuntimeEventObserver) -> Iterator[None]:
    """Observe Runtime V2 events encoded in the current task context."""

    current = _RUNTIME_EVENT_OBSERVERS.get()
    token = _RUNTIME_EVENT_OBSERVERS.set((*current, observer))
    try:
        yield
    finally:
        _RUNTIME_EVENT_OBSERVERS.reset(token)


def _notify_runtime_event_observers(event: RuntimeEvent, wire: str) -> None:
    for observer in _RUNTIME_EVENT_OBSERVERS.get():
        observer(event, wire)


class RuntimeEventFactory:
    """Run-scoped monotonic event sequence; safe for progress callbacks."""

    def __init__(
        self,
        run_id: str,
        *,
        conversation_id: str,
        turn_id: str,
    ):
        self.run_id = str(run_id)
        self.conversation_id = str(conversation_id or "")
        self.turn_id = str(turn_id or "")
        if not all((self.run_id, self.conversation_id, self.turn_id)):
            raise ValueError(
                "runtime event factory requires run, conversation, and turn identity"
            )
        self._candidate_id: Optional[str] = None
        self._sequence = 0
        self._lock = threading.Lock()

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def create(
        self,
        event_type: str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        caused_by: Optional[str] = None,
    ) -> RuntimeEvent:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        return RuntimeEvent(
            version=2,
            event_id=secrets.token_urlsafe(18),
            run_id=self.run_id,
            conversation_id=self.conversation_id,
            turn_id=self.turn_id,
            candidate_id=self._candidate_id,
            sequence=sequence,
            timestamp=datetime.now(timezone.utc).isoformat(),
            type=str(event_type),
            caused_by=str(caused_by) if caused_by else None,
            payload=MappingProxyType(dict(payload or {})),
        )

    def select_candidate(self, candidate_id: Optional[str]) -> None:
        with self._lock:
            self._candidate_id = str(candidate_id) if candidate_id else None


def encode_runtime_sse(event: RuntimeEvent) -> str:
    wire = f"data: {json.dumps(event.as_dict(), separators=(',', ':'))}\n\n"
    _notify_runtime_event_observers(event, wire)
    return wire


def runtime_event_from_payload(payload: Mapping[str, Any]) -> Optional[RuntimeEvent]:
    if payload.get("version") != 2:
        return None
    required = {
        "event_id",
        "run_id",
        "conversation_id",
        "turn_id",
        "candidate_id",
        "sequence",
        "timestamp",
        "type",
        "caused_by",
        "payload",
    }
    if not required.issubset(payload):
        return None
    nested = payload.get("payload")
    if not isinstance(nested, Mapping):
        return None
    if not str(payload.get("run_id") or ""):
        return None
    if not str(payload.get("conversation_id") or ""):
        return None
    if not str(payload.get("turn_id") or ""):
        return None
    try:
        sequence = int(payload["sequence"])
    except (TypeError, ValueError):
        return None
    return RuntimeEvent(
        version=2,
        event_id=str(payload["event_id"]),
        run_id=str(payload["run_id"]),
        conversation_id=str(payload["conversation_id"]),
        turn_id=str(payload["turn_id"]),
        candidate_id=(
            str(payload["candidate_id"])
            if payload.get("candidate_id") is not None
            else None
        ),
        sequence=sequence,
        timestamp=str(payload["timestamp"]),
        type=str(payload["type"]),
        caused_by=(
            str(payload["caused_by"])
            if payload.get("caused_by") is not None
            else None
        ),
        payload=MappingProxyType(dict(nested)),
    )


__all__ = [
    "RuntimeEvent",
    "RuntimeEventFactory",
    "RuntimeEventObserver",
    "encode_runtime_sse",
    "observe_runtime_events",
    "runtime_event_from_payload",
]
