"""Versioned, ordered Runtime V2 events and their SSE projection."""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class RuntimeEvent:
    version: int
    event_id: str
    run_id: str
    conversation_id: Optional[str]
    turn_id: Optional[str]
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


class RuntimeEventFactory:
    """Run-scoped monotonic event sequence; safe for progress callbacks."""

    def __init__(
        self,
        run_id: str,
        *,
        conversation_id: Optional[str] = None,
        turn_id: Optional[str] = None,
    ):
        self.run_id = str(run_id)
        self.conversation_id = str(conversation_id) if conversation_id else None
        self.turn_id = str(turn_id) if turn_id else None
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
    return f"data: {json.dumps(event.as_dict(), separators=(',', ':'))}\n\n"


def runtime_event_from_payload(payload: Mapping[str, Any]) -> Optional[RuntimeEvent]:
    if payload.get("version") != 2:
        return None
    required = {
        "event_id",
        "run_id",
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
    try:
        sequence = int(payload["sequence"])
    except (TypeError, ValueError):
        return None
    return RuntimeEvent(
        version=2,
        event_id=str(payload["event_id"]),
        run_id=str(payload["run_id"]),
        conversation_id=(
            str(payload["conversation_id"])
            if payload.get("conversation_id") is not None
            else None
        ),
        turn_id=(
            str(payload["turn_id"])
            if payload.get("turn_id") is not None
            else None
        ),
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
