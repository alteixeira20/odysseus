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
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "type": self.type,
            "caused_by": self.caused_by,
            "payload": dict(self.payload),
        }


class RuntimeEventFactory:
    """Run-scoped monotonic event sequence; safe for progress callbacks."""

    def __init__(self, run_id: str):
        self.run_id = str(run_id)
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
            sequence=sequence,
            timestamp=datetime.now(timezone.utc).isoformat(),
            type=str(event_type),
            caused_by=str(caused_by) if caused_by else None,
            payload=MappingProxyType(dict(payload or {})),
        )


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
