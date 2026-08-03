from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {
            self.COMPLETED,
            self.INCOMPLETE,
            self.CANCELLED,
            self.FAILED,
            self.INTERRUPTED,
        }


class EffectStatus(str, Enum):
    NOT_STARTED = "not_started"
    STARTED = "started"
    COMMITTED = "committed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class EffectClass(str, Enum):
    READ = "read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    PROCESS = "process"
    UNKNOWN = "unknown"


class RetryPolicy(str, Enum):
    SAFE = "safe"
    IDEMPOTENCY_KEY_REQUIRED = "idempotency_key_required"
    NEVER = "never"


@dataclass(frozen=True)
class EffectLease:
    effect_id: str
    should_execute: bool
    status: EffectStatus
    cached_result: Mapping[str, Any] | None = None
    reason: str = ""


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    session_id: str | None
    owner: str | None
    status: RunStatus
    revision: int
    last_event_seq: int
    terminal_reason: str | None
    resumable: bool
