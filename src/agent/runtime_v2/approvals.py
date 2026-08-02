"""Exact, one-use approval records for sensitive Runtime V2 effects."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

from .contracts import AgentExecutionContext, Effect, NormalizedToolCall
from .ownership import OwnershipError, RUN_OWNERSHIP
from .workspace_service import WORKSPACE_SERVICE


class ApprovalRecordState(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    CLAIMED = "claimed"
    SPAWNING = "spawning"
    EXECUTING = "executing"
    DETACHED_EXECUTING = "detached_executing"
    DETACHED_COMPLETED = "detached_completed"
    DETACHED_FAILED = "detached_failed"
    DETACHED_TIMED_OUT = "detached_timed_out"
    DETACHED_CANCELLED = "detached_cancelled"
    COMPLETED = "completed"
    COMMITTED = "committed"
    FAILED_BEFORE_EFFECT = "failed_before_effect"
    FAILED_AFTER_UNKNOWN_EFFECT = "failed_after_unknown_effect"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"


class EffectApprovalError(RuntimeError):
    code = "effect_approval_invalid"

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        if code:
            self.code = str(code)


@dataclass(frozen=True)
class ApprovalRequest:
    approval_id: str
    expires_at: float
    call_id: str
    canonical_name: str
    effects_digest: str


@dataclass
class _ApprovalRecord:
    approval_id: str
    owner_id: str
    session_id: str
    run_id: str
    conversation_id: str
    turn_id: str
    candidate_id: str
    call_id: str
    canonical_name: str
    arguments_digest: str
    effects_digest: str
    authority_revision: str
    tool_contract_revision: str
    workspace_root: str
    workspace_revision: str
    expires_at: float
    state: ApprovalRecordState = ApprovalRecordState.PENDING
    claim_count: int = 0
    executing_at: Optional[float] = None
    finished_at: Optional[float] = None


def _effects_digest(effects: Sequence[Effect]) -> str:
    payload = [
        {
            "kind": effect.kind,
            "target": effect.target,
            "capability": effect.capability.value,
            "consequential": effect.consequential,
            "destructive": effect.destructive,
            "opaque": effect.opaque,
            "metadata": dict(effect.metadata),
        }
        for effect in effects
    ]
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


class EffectApprovalStore:
    """Process-local lifecycle; deployment is guarded to one runtime worker."""

    def __init__(self, *, ttl_seconds: float = 120.0) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self._records: dict[str, _ApprovalRecord] = {}
        self._lock = threading.RLock()

    def request(
        self,
        *,
        call: NormalizedToolCall,
        context: AgentExecutionContext,
        effects: Sequence[Effect],
    ) -> ApprovalRequest:
        RUN_OWNERSHIP.validate_call(context, call)
        workspace_revision = WORKSPACE_SERVICE.revision(
            context.execution_root.path
        )
        if not WORKSPACE_SERVICE.revision_is_strong(workspace_revision):
            raise EffectApprovalError(
                "workspace identity exceeded its approval safety budget"
            )
        approval_id = secrets.token_urlsafe(24)
        expires_at = time.time() + self.ttl_seconds
        digest = _effects_digest(effects)
        record = _ApprovalRecord(
            approval_id=approval_id,
            owner_id=context.owner_id,
            session_id=context.session_id,
            run_id=context.run_id,
            conversation_id=context.conversation_id,
            turn_id=context.turn_id,
            candidate_id=call.candidate_id,
            call_id=call.call_id,
            canonical_name=call.canonical_name,
            arguments_digest=call.arguments_digest,
            effects_digest=digest,
            authority_revision=context.authority_grant.revision,
            tool_contract_revision=call.tool_contract_revision,
            workspace_root=context.execution_root.path,
            workspace_revision=workspace_revision,
            expires_at=expires_at,
        )
        with self._lock:
            self._prune_locked(time.time())
            self._records[approval_id] = record
        return ApprovalRequest(
            approval_id=approval_id,
            expires_at=expires_at,
            call_id=call.call_id,
            canonical_name=call.canonical_name,
            effects_digest=digest,
        )

    def decide(
        self,
        approval_id: str,
        *,
        owner_id: str,
        decision: str,
    ) -> ApprovalRecordState:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            record = self._records.get(str(approval_id))
            if record is None or record.owner_id != str(owner_id or ""):
                raise EffectApprovalError(
                    "approval request was not found",
                    code="effect_approval_invalidated",
                )
            if record.state is not ApprovalRecordState.PENDING:
                code = {
                    ApprovalRecordState.DENIED: "effect_approval_denied",
                    ApprovalRecordState.EXPIRED: "effect_approval_expired",
                    ApprovalRecordState.INVALIDATED: "effect_approval_invalidated",
                }.get(record.state, "effect_approval_state_conflict")
                raise EffectApprovalError(
                    f"approval request is already {record.state.value}",
                    code=code,
                )
            if not RUN_OWNERSHIP.snapshot_is_current(
                owner_id=record.owner_id,
                conversation_id=record.conversation_id,
                turn_id=record.turn_id,
                run_id=record.run_id,
            ):
                record.state = ApprovalRecordState.INVALIDATED
                raise EffectApprovalError(
                    "approval request belongs to a superseded run or turn",
                    code="effect_approval_invalidated",
                )
            try:
                current_revision = WORKSPACE_SERVICE.revision(
                    record.workspace_root
                )
            except Exception as exc:
                record.state = ApprovalRecordState.INVALIDATED
                raise EffectApprovalError(
                    "approval workspace is no longer available",
                    code="effect_approval_invalidated",
                ) from exc
            if current_revision != record.workspace_revision:
                record.state = ApprovalRecordState.INVALIDATED
                raise EffectApprovalError(
                    "workspace changed after approval was requested",
                    code="effect_approval_invalidated",
                )
            normalized = str(decision or "").strip().lower()
            if normalized == "allow":
                record.state = ApprovalRecordState.GRANTED
            elif normalized == "deny":
                record.state = ApprovalRecordState.DENIED
            else:
                raise EffectApprovalError("decision must be allow or deny")
            return record.state

    async def wait(
        self,
        approval_id: str,
        *,
        context: AgentExecutionContext,
        poll_seconds: float = 0.05,
    ) -> ApprovalRecordState:
        while True:
            context.cancellation_token.raise_if_cancelled()
            with self._lock:
                self._prune_locked(time.time())
                record = self._records.get(str(approval_id))
                if record is None:
                    return ApprovalRecordState.INVALIDATED
                state = record.state
            if state is not ApprovalRecordState.PENDING:
                return state
            await asyncio.sleep(max(float(poll_seconds), 0.01))

    def claim(
        self,
        approval_id: str,
        *,
        call: NormalizedToolCall,
        context: AgentExecutionContext,
        effects: Sequence[Effect],
    ) -> ApprovalRecordState:
        try:
            RUN_OWNERSHIP.validate_call(context, call)
        except OwnershipError as exc:
            raise EffectApprovalError(str(exc)) from exc
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            record = self._records.get(str(approval_id))
            if record is None:
                raise EffectApprovalError(
                    "approval request was not found or expired",
                    code="effect_approval_expired",
                )
            if record.state not in {
                ApprovalRecordState.GRANTED,
                ApprovalRecordState.FAILED_BEFORE_EFFECT,
            }:
                code = {
                    ApprovalRecordState.DENIED: "effect_approval_denied",
                    ApprovalRecordState.EXPIRED: "effect_approval_expired",
                    ApprovalRecordState.INVALIDATED: "effect_approval_invalidated",
                    ApprovalRecordState.CLAIMED: "effect_approval_already_claimed",
                    ApprovalRecordState.SPAWNING: "effect_outcome_unknown_or_active",
                    ApprovalRecordState.EXECUTING: "effect_outcome_unknown_or_active",
                    ApprovalRecordState.DETACHED_EXECUTING: "effect_outcome_unknown_or_active",
                    ApprovalRecordState.DETACHED_COMPLETED: "effect_approval_already_completed",
                    ApprovalRecordState.DETACHED_FAILED: "effect_outcome_unknown",
                    ApprovalRecordState.DETACHED_TIMED_OUT: "effect_outcome_unknown",
                    ApprovalRecordState.DETACHED_CANCELLED: "effect_outcome_unknown",
                    ApprovalRecordState.COMPLETED: "effect_approval_already_completed",
                    ApprovalRecordState.COMMITTED: "effect_approval_already_committed",
                    ApprovalRecordState.FAILED_AFTER_UNKNOWN_EFFECT: "effect_outcome_unknown",
                }.get(record.state, "effect_approval_invalid")
                raise EffectApprovalError(
                    f"approval request is {record.state.value}, not claimable",
                    code=code,
                )
            try:
                current_workspace_revision = WORKSPACE_SERVICE.revision(
                    context.execution_root.path
                )
            except Exception as exc:
                record.state = ApprovalRecordState.INVALIDATED
                raise EffectApprovalError(
                    "approval workspace is no longer available",
                    code="effect_approval_invalidated",
                ) from exc
            exact = (
                record.owner_id == context.owner_id
                and record.session_id == context.session_id
                and record.run_id == context.run_id
                and record.conversation_id == context.conversation_id
                and record.turn_id == context.turn_id
                and record.candidate_id == call.candidate_id
                and record.call_id == call.call_id
                and record.canonical_name == call.canonical_name
                and record.arguments_digest == call.arguments_digest
                and record.effects_digest == _effects_digest(effects)
                and record.authority_revision == context.authority_grant.revision
                and record.tool_contract_revision == call.tool_contract_revision
                and record.workspace_root == context.execution_root.path
                and record.workspace_revision
                == current_workspace_revision
            )
            if not exact:
                record.state = ApprovalRecordState.INVALIDATED
                raise EffectApprovalError(
                    "approval no longer matches the exact call, effects, authority, or workspace",
                    code="effect_approval_invalidated",
                )
            # This compare-and-set is the one active execution lease. A launch
            # that failed before the boundary may be reclaimed; a started effect
            # can never be replayed.
            record.state = ApprovalRecordState.CLAIMED
            record.claim_count += 1
            return record.state

    # Compatibility spelling for internal callers during the Runtime V2
    # transition. New code should use claim and complete the lifecycle.
    consume = claim

    def mark_spawning(
        self,
        approval_id: str,
        *,
        call: NormalizedToolCall,
        context: AgentExecutionContext,
        effects: Sequence[Effect],
    ) -> ApprovalRecordState:
        """Atomically burn replayability immediately before kernel spawn.

        This transition is intentionally not described as atomic with process
        creation.  ``SPAWNING`` is the uncertainty state for that unavoidable
        gap: cancellation or failure from here is retryable only when the spawn
        owner proves that no child was created.
        """

        context.cancellation_token.raise_if_cancelled()
        try:
            RUN_OWNERSHIP.validate_call(context, call)
        except OwnershipError as exc:
            raise EffectApprovalError(str(exc)) from exc
        with self._lock:
            record = self._records.get(str(approval_id))
            if record is None:
                raise EffectApprovalError("approval request was not found")
            if record.state is not ApprovalRecordState.CLAIMED:
                raise EffectApprovalError(
                    f"approval request cannot spawn from {record.state.value}",
                    code="effect_approval_state_conflict",
                )
            exact = (
                record.owner_id == context.owner_id
                and record.session_id == context.session_id
                and record.run_id == context.run_id
                and record.conversation_id == context.conversation_id
                and record.turn_id == context.turn_id
                and record.candidate_id == call.candidate_id
                and record.call_id == call.call_id
                and record.canonical_name == call.canonical_name
                and record.arguments_digest == call.arguments_digest
                and record.effects_digest == _effects_digest(effects)
                and record.authority_revision == context.authority_grant.revision
                and record.tool_contract_revision == call.tool_contract_revision
                and record.workspace_root == context.execution_root.path
                and record.workspace_revision
                == WORKSPACE_SERVICE.revision(context.execution_root.path)
            )
            if not exact:
                record.state = ApprovalRecordState.INVALIDATED
                raise EffectApprovalError(
                    "approval no longer matches at the process-spawn boundary",
                    code="effect_approval_invalidated",
                )
            record.state = ApprovalRecordState.SPAWNING
            record.executing_at = time.time()
            return record.state

    def mark_executing(self, approval_id: str) -> ApprovalRecordState:
        with self._lock:
            record = self._records.get(str(approval_id))
            if record is None:
                raise EffectApprovalError("approval request was not found")
            if record.state not in {
                ApprovalRecordState.CLAIMED,
                ApprovalRecordState.SPAWNING,
            }:
                raise EffectApprovalError(
                    f"approval request cannot start from {record.state.value}",
                    code="effect_approval_state_conflict",
                )
            record.state = ApprovalRecordState.EXECUTING
            record.executing_at = time.time()
            return record.state

    def complete(
        self,
        approval_id: str,
        *,
        committed_effects: bool = False,
    ) -> ApprovalRecordState:
        with self._lock:
            record = self._records.get(str(approval_id))
            if record is None:
                raise EffectApprovalError("approval request was not found")
            if record.state not in {
                ApprovalRecordState.CLAIMED,
                ApprovalRecordState.EXECUTING,
            }:
                raise EffectApprovalError(
                    f"approval request cannot complete from {record.state.value}",
                    code="effect_approval_state_conflict",
                )
            # A successfully reaped opaque process proves execution completed;
            # it does not prove every predicted side effect committed.  Only a
            # handler that returned validated committed effects earns the
            # COMMITTED lifecycle state.
            record.state = (
                ApprovalRecordState.COMMITTED
                if committed_effects
                else ApprovalRecordState.COMPLETED
            )
            record.finished_at = time.time()
            return record.state

    def fail_before_effect(
        self,
        approval_id: str,
        *,
        process_creation_proven_absent: bool = False,
    ) -> ApprovalRecordState:
        with self._lock:
            record = self._records.get(str(approval_id))
            if record is None:
                raise EffectApprovalError("approval request was not found")
            allowed = record.state is ApprovalRecordState.CLAIMED or (
                record.state is ApprovalRecordState.SPAWNING
                and process_creation_proven_absent
            )
            if not allowed:
                raise EffectApprovalError(
                    f"approval request cannot record a pre-effect failure from {record.state.value}",
                    code="effect_approval_state_conflict",
                )
            record.state = ApprovalRecordState.FAILED_BEFORE_EFFECT
            record.finished_at = time.time()
            return record.state

    def fail_after_unknown_effect(self, approval_id: str) -> ApprovalRecordState:
        with self._lock:
            record = self._records.get(str(approval_id))
            if record is None:
                raise EffectApprovalError("approval request was not found")
            if record.state not in {
                ApprovalRecordState.SPAWNING,
                ApprovalRecordState.EXECUTING,
                ApprovalRecordState.DETACHED_EXECUTING,
            }:
                raise EffectApprovalError(
                    f"approval request cannot record an uncertain effect from {record.state.value}",
                    code="effect_approval_state_conflict",
                )
            record.state = ApprovalRecordState.FAILED_AFTER_UNKNOWN_EFFECT
            record.finished_at = time.time()
            return record.state

    def mark_detached_executing(self, approval_id: str) -> ApprovalRecordState:
        with self._lock:
            record = self._records.get(str(approval_id))
            if record is None:
                raise EffectApprovalError("approval request was not found")
            if record.state is not ApprovalRecordState.EXECUTING:
                raise EffectApprovalError(
                    f"approval request cannot detach from {record.state.value}",
                    code="effect_approval_state_conflict",
                )
            record.state = ApprovalRecordState.DETACHED_EXECUTING
            return record.state

    def finalize_detached(
        self,
        approval_id: str,
        outcome: ApprovalRecordState | str,
    ) -> ApprovalRecordState:
        final = ApprovalRecordState(outcome)
        if final not in {
            ApprovalRecordState.DETACHED_COMPLETED,
            ApprovalRecordState.DETACHED_FAILED,
            ApprovalRecordState.DETACHED_TIMED_OUT,
            ApprovalRecordState.DETACHED_CANCELLED,
        }:
            raise EffectApprovalError("invalid detached approval outcome")
        with self._lock:
            record = self._records.get(str(approval_id))
            if record is None:
                raise EffectApprovalError("approval request was not found")
            if record.state is not ApprovalRecordState.DETACHED_EXECUTING:
                if record.state is final:
                    return final
                raise EffectApprovalError(
                    f"approval request cannot finalize detached work from {record.state.value}",
                    code="effect_approval_state_conflict",
                )
            record.state = final
            record.finished_at = time.time()
            return final

    def state(self, approval_id: str) -> Optional[ApprovalRecordState]:
        with self._lock:
            self._prune_locked(time.time())
            record = self._records.get(str(approval_id))
            return record.state if record else None

    def invalidate(self, approval_id: str) -> None:
        """Conservatively burn a pending/granted record after snapshot drift."""

        with self._lock:
            record = self._records.get(str(approval_id))
            if record and record.state in {
                ApprovalRecordState.PENDING,
                ApprovalRecordState.GRANTED,
                ApprovalRecordState.FAILED_BEFORE_EFFECT,
            }:
                record.state = ApprovalRecordState.INVALIDATED

    def _prune_locked(self, now: float) -> None:
        for record in self._records.values():
            if (
                record.state in {
                    ApprovalRecordState.PENDING,
                    ApprovalRecordState.GRANTED,
                    ApprovalRecordState.FAILED_BEFORE_EFFECT,
                }
                and record.expires_at <= now
            ):
                record.state = ApprovalRecordState.EXPIRED

    def clear_for_tests(self) -> None:
        with self._lock:
            self._records.clear()


EFFECT_APPROVALS = EffectApprovalStore()


__all__ = [
    "ApprovalRecordState",
    "ApprovalRequest",
    "EFFECT_APPROVALS",
    "EffectApprovalError",
    "EffectApprovalStore",
]
