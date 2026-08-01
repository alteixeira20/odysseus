"""Process-local run, turn, and provider-candidate ownership leases.

Task cancellation is only a delivery mechanism.  These leases are checked at
event-commit and tool-dispatch boundaries so a cancelled coroutine that runs
late cannot publish output or perform an effect for a superseded turn.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from typing import Optional


class OwnershipError(RuntimeError):
    code = "ownership_rejected"


@dataclass(frozen=True)
class TurnLease:
    conversation_id: str
    turn_id: str


@dataclass
class _TurnRecord:
    owner_id: str
    conversation_id: str
    turn_id: str
    run_id: Optional[str] = None
    run_active: bool = False
    selected_candidates: dict[int, str] = field(default_factory=dict)
    effective_tools: frozenset[str] = frozenset()
    tool_contract_revision: str = "unbound"


class RunOwnershipStore:
    """Single-process compare-and-set ownership registry."""

    def __init__(self) -> None:
        self._records: dict[str, _TurnRecord] = {}
        self._lock = threading.RLock()

    def claim_turn(self, *, owner_id: str, conversation_id: str) -> TurnLease:
        conversation = str(conversation_id or "")
        owner = str(owner_id or "")
        if not conversation:
            raise ValueError("turn ownership requires a conversation id")
        lease = TurnLease(conversation, secrets.token_urlsafe(18))
        with self._lock:
            self._records[conversation] = _TurnRecord(
                owner_id=owner,
                conversation_id=conversation,
                turn_id=lease.turn_id,
            )
        return lease

    def bind_run(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        turn_id: str,
        run_id: str,
    ) -> None:
        with self._lock:
            record = self._records.get(str(conversation_id))
            if record is None:
                raise OwnershipError("turn lease is no longer current")
            if (
                record.owner_id != str(owner_id or "")
                or record.turn_id != str(turn_id or "")
            ):
                raise OwnershipError("run cannot bind a superseded or foreign turn")
            if record.run_id is not None and record.run_id != str(run_id):
                raise OwnershipError("turn already has an authoritative run")
            record.run_id = str(run_id)
            record.run_active = True

    def end_run(self, context) -> None:
        """Atomically revoke final-boundary authority for a terminal run."""

        with self._lock:
            record = self._records.get(str(context.conversation_id))
            if (
                record is not None
                and record.owner_id == str(context.owner_id or "")
                and record.turn_id == str(context.turn_id)
                and record.run_id == str(context.run_id)
            ):
                record.run_active = False

    def bind_effective_tools(
        self,
        context,
        *,
        names: frozenset[str],
        revision: str,
    ) -> None:
        with self._lock:
            record = self._require_context_locked(context)
            record.effective_tools = frozenset(str(name) for name in names)
            record.tool_contract_revision = str(revision)

    def select_candidate(self, context, *, round_number: int, candidate_id: str) -> None:
        candidate = str(candidate_id or "")
        if not candidate:
            raise OwnershipError("provider candidate identity is missing")
        round_id = int(round_number)
        with self._lock:
            record = self._require_context_locked(context)
            selected = record.selected_candidates.get(round_id)
            if selected is not None and selected != candidate:
                raise OwnershipError("a provider candidate is already authoritative for this round")
            record.selected_candidates[round_id] = candidate

    def validate_context(self, context) -> None:
        with self._lock:
            self._require_context_locked(context)

    def validate_call(self, context, call) -> None:
        self.validate_call_identity(context, call)
        self.validate_effective_tool(context, call.canonical_name)

    def validate_call_identity(self, context, call) -> None:
        with self._lock:
            record = self._require_context_locked(context)
            if call.run_id != context.run_id or call.turn_id != context.turn_id:
                raise OwnershipError("tool call belongs to a different run or turn")
            if call.conversation_id != context.conversation_id:
                raise OwnershipError("tool call belongs to a different conversation")
            if call.authority_revision != context.authority_grant.revision:
                raise OwnershipError("tool call authority snapshot changed")
            from .workspace_service import WORKSPACE_SERVICE

            if call.workspace_revision != WORKSPACE_SERVICE.revision(
                context.execution_root.path
            ):
                raise OwnershipError("tool call workspace snapshot changed")
            if call.tool_contract_revision != record.tool_contract_revision:
                raise OwnershipError("tool call effective-tool contract changed")
            if record.selected_candidates.get(int(call.provider_round)) != call.candidate_id:
                raise OwnershipError("tool call came from a losing or unselected candidate")

    def validate_effective_tool(self, context, canonical_name: str) -> None:
        with self._lock:
            record = self._require_context_locked(context)
            if str(canonical_name) not in record.effective_tools:
                raise OwnershipError(
                    f"tool is outside the effective contract: {canonical_name}"
                )

    def validate_event(self, context, event) -> None:
        with self._lock:
            record = self._require_context_locked(context)
            if event.run_id != context.run_id:
                raise OwnershipError("runtime event belongs to a different run")
            if event.conversation_id not in {None, context.conversation_id}:
                raise OwnershipError("runtime event belongs to a different conversation")
            if event.turn_id not in {None, context.turn_id}:
                raise OwnershipError("runtime event belongs to a superseded turn")
            if event.candidate_id is not None and (
                event.candidate_id not in record.selected_candidates.values()
            ):
                raise OwnershipError("runtime event came from a losing candidate")

    def effective_tools(self, context) -> frozenset[str]:
        with self._lock:
            return self._require_context_locked(context).effective_tools

    def tool_contract_revision(self, context) -> str:
        with self._lock:
            return self._require_context_locked(context).tool_contract_revision

    def snapshot_is_current(
        self,
        *,
        owner_id: str,
        conversation_id: str,
        turn_id: str,
        run_id: str,
    ) -> bool:
        """Read-only check for detached work before it may auto-continue."""

        with self._lock:
            record = self._records.get(str(conversation_id))
            return bool(
                record
                and record.owner_id == str(owner_id or "")
                and record.turn_id == str(turn_id or "")
                and record.run_id == str(run_id or "")
                and record.run_active
            )

    def _require_context_locked(self, context) -> _TurnRecord:
        record = self._records.get(str(context.conversation_id))
        if record is None:
            raise OwnershipError("conversation has no current turn")
        if record.owner_id != str(context.owner_id or ""):
            raise OwnershipError("run owner is no longer authoritative")
        if record.turn_id != str(context.turn_id):
            raise OwnershipError("turn was superseded by a newer user message")
        if record.run_id != str(context.run_id):
            raise OwnershipError("run is no longer authoritative")
        if not record.run_active:
            raise OwnershipError("run is already terminal")
        return record

    def clear_for_tests(self) -> None:
        with self._lock:
            self._records.clear()


RUN_OWNERSHIP = RunOwnershipStore()


__all__ = ["OwnershipError", "RUN_OWNERSHIP", "RunOwnershipStore", "TurnLease"]
