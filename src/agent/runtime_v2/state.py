"""Authoritative Runtime V2 run-state reducer."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .events import RuntimeEvent


class RunState(str, Enum):
    CREATED = "created"
    PREPARING = "preparing"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"
    INCOMPLETE = "incomplete"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            RunState.WAITING_USER,
            RunState.INCOMPLETE,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
        }


_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset(
        {
            RunState.PREPARING,
            RunState.WAITING_APPROVAL,
            RunState.WAITING_USER,
            RunState.INCOMPLETE,
            RunState.COMPLETED,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.PREPARING: frozenset(
        {
            RunState.RUNNING,
            RunState.WAITING_APPROVAL,
            RunState.WAITING_USER,
            RunState.INCOMPLETE,
            RunState.COMPLETED,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.WAITING_APPROVAL,
            RunState.WAITING_USER,
            RunState.INCOMPLETE,
            RunState.COMPLETED,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.WAITING_APPROVAL: frozenset(
        {
            RunState.RUNNING,
            RunState.INCOMPLETE,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.WAITING_USER: frozenset(),
    RunState.INCOMPLETE: frozenset(),
    RunState.COMPLETED: frozenset({RunState.CANCELLED, RunState.FAILED}),
    RunState.FAILED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


@dataclass
class RunStateMachine:
    run_id: str
    state: RunState = RunState.CREATED
    reason: str = "created"
    last_sequence: int = 0

    def transition(
        self,
        state: RunState,
        *,
        reason: str,
        sequence: Optional[int] = None,
        cancellation_override: bool = False,
    ) -> None:
        if sequence is not None:
            if sequence <= self.last_sequence:
                raise ValueError(
                    f"out-of-order runtime event sequence {sequence} <= {self.last_sequence}"
                )
            self.last_sequence = sequence
        if state is self.state:
            self.reason = str(reason)
            return
        if cancellation_override and state is RunState.CANCELLED:
            self.state = state
            self.reason = str(reason)
            return
        if state not in _TRANSITIONS[self.state]:
            raise ValueError(
                f"invalid run-state transition {self.state.value} -> {state.value}"
            )
        self.state = state
        self.reason = str(reason)

    def reduce(self, event: RuntimeEvent) -> bool:
        if event.run_id != self.run_id or event.type != "run_state":
            return False
        try:
            state = RunState(str(event.payload.get("state")))
        except ValueError:
            state = RunState.INCOMPLETE
        self.transition(
            state,
            reason=str(event.payload.get("reason") or state.value),
            sequence=event.sequence,
        )
        return True
