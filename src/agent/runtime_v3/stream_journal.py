"""Bounded durable journaling for high-frequency agent stream events.

User-visible SSE delivery remains immediate. Only explicitly transient stream
signals are coalesced before SQLite persistence; lifecycle/effect/approval/error
signals and every unknown event type are durably appended one-by-one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json

from .ledger import DurableRunLedger


# This allowlist is deliberately small. Adding a type here is a durability
# decision: it may be lost if the process dies before the current batch flushes.
# Unknown/new event types therefore fail safe by remaining immediate.
TRANSIENT_EVENT_TYPES = frozenset(
    {
        "delta",
        "agent_step",
        "tool_progress",
    }
)


def _encoded_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    )


@dataclass
class DurableStreamJournal:
    ledger: DurableRunLedger
    run_id: str
    max_event_bytes: int
    max_batch_events: int = 32
    max_batch_bytes: int = 128 * 1024
    _items: list[dict[str, str]] = field(default_factory=list, init=False)
    _bytes: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.max_event_bytes = max(1024, int(self.max_event_bytes))
        self.max_batch_events = max(1, int(self.max_batch_events))
        # Leave room for the JSON list/wrapper so a legal batch cannot cross the
        # ledger's per-event byte ceiling merely because of framing overhead.
        ceiling = max(1024, self.max_event_bytes - 512)
        self.max_batch_bytes = max(
            1024,
            min(int(self.max_batch_bytes), ceiling),
        )

    @property
    def pending_count(self) -> int:
        return len(self._items)

    def record(self, event_type: str, wire: str) -> None:
        event_type = str(event_type or "wire")
        if event_type not in TRANSIENT_EVENT_TYPES:
            self.flush()
            self.ledger.append_event(
                self.run_id,
                event_type,
                {"wire": wire},
                max_bytes=self.max_event_bytes,
            )
            return

        item = {"type": event_type, "wire": wire}
        item_bytes = _encoded_size(item)
        if item_bytes > self.max_batch_bytes:
            # A single large delta cannot be safely combined, but it still
            # follows the old per-event persistence path and byte ceiling.
            self.flush()
            self.ledger.append_event(
                self.run_id,
                event_type,
                {"wire": wire},
                max_bytes=self.max_event_bytes,
            )
            return

        if self._items and (
            len(self._items) >= self.max_batch_events
            or self._bytes + item_bytes > self.max_batch_bytes
        ):
            self.flush()

        self._items.append(item)
        self._bytes += item_bytes
        if (
            len(self._items) >= self.max_batch_events
            or self._bytes >= self.max_batch_bytes
        ):
            self.flush()

    def flush(self) -> None:
        if not self._items:
            return
        items = self._items
        self._items = []
        self._bytes = 0
        self.ledger.append_event(
            self.run_id,
            "stream_batch",
            {"events": items},
            max_bytes=self.max_event_bytes,
        )


__all__ = ["DurableStreamJournal", "TRANSIENT_EVENT_TYPES"]
