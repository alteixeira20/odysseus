from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping

from src.agent.runtime_v2.contracts import (
    AgentExecutionContext,
    Capability,
    Effect,
    NormalizedToolCall,
    ToolError,
    ToolResult,
    ToolResultStatus,
)

from .contracts import EffectClass, EffectStatus, RetryPolicy
from .ledger import DurableRunLedger, get_runtime_ledger


_SENSITIVE_KEY = re.compile(
    r"(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|passwd|cookie|private[_-]?key)",
    re.IGNORECASE,
)
_RESULT_INLINE_LIMIT = 256 * 1024


@dataclass(frozen=True)
class DurableEffectHandle:
    ledger: DurableRunLedger
    run_id: str
    effect_id: str
    effect_class: EffectClass
    retry_policy: RetryPolicy
    should_execute: bool
    replay_result: ToolResult | None = None
    blocked_reason: str = ""


def _secret_fingerprint(value: Any) -> dict[str, str]:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return {"$redacted_sha256": hashlib.sha256(raw).hexdigest()}


def redact_sensitive(value: Any, *, parent_key: str = "") -> Any:
    if parent_key and _SENSITIVE_KEY.search(parent_key):
        return _secret_fingerprint(value)
    if isinstance(value, Mapping):
        return {str(key): redact_sensitive(item, parent_key=str(key)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_sensitive(item) for item in value]
    return value


def _classify_effects(effects: Iterable[Effect]) -> tuple[EffectClass, RetryPolicy]:
    items = tuple(effects)
    if not items:
        return EffectClass.READ, RetryPolicy.SAFE
    if any(effect.kind.startswith("process.") or effect.kind.startswith("process_") for effect in items):
        return EffectClass.PROCESS, RetryPolicy.NEVER
    if any(
        effect.capability in {Capability.EXTERNAL_WRITE, Capability.NETWORK_PRIVATE, Capability.CREDENTIAL_ACCESS}
        or (effect.consequential and not effect.kind.startswith(("filesystem.", "vcs.")))
        or effect.opaque
        for effect in items
    ):
        return EffectClass.EXTERNAL_WRITE, RetryPolicy.NEVER
    if any(
        effect.capability in {Capability.WORKSPACE_WRITE, Capability.PROCESS_WORKSPACE_WRITE, Capability.VCS_WRITE}
        or effect.consequential
        or effect.destructive
        for effect in items
    ):
        return EffectClass.LOCAL_WRITE, RetryPolicy.IDEMPOTENCY_KEY_REQUIRED
    return EffectClass.READ, RetryPolicy.SAFE


def _ensure_run(ledger: DurableRunLedger, context: AgentExecutionContext) -> None:
    if ledger.get_run(context.run_id) is not None:
        return
    ledger.create_run(
        run_id=context.run_id,
        session_id=context.session_id,
        owner=context.owner_id,
        workload="runtime_v2_tool",
        request={
            "source": "runtime_v2_executor",
            "conversation_id": context.conversation_id,
            "turn_id": context.turn_id,
            "candidate_id": context.candidate_id,
            "execution_root": context.execution_root.path,
            "authority_revision": context.authority_grant.revision,
        },
        limits=asdict(context.budgets),
        model=None,
        endpoint=None,
    )
    from .contracts import RunStatus

    ledger.transition(context.run_id, RunStatus.RUNNING, reason="runtime_v2_tool_started")


def _effect_from_dict(payload: Mapping[str, Any]) -> Effect:
    return Effect(
        kind=str(payload.get("kind") or "unknown"),
        target=str(payload.get("target") or "unknown"),
        capability=Capability(str(payload.get("capability") or Capability.EXTERNAL_READ.value)),
        consequential=bool(payload.get("consequential")),
        destructive=bool(payload.get("destructive")),
        opaque=bool(payload.get("opaque")),
        metadata=payload.get("metadata") or {},
    )


def tool_result_from_dict(payload: Mapping[str, Any]) -> ToolResult:
    error_payload = payload.get("error")
    error = None
    if isinstance(error_payload, Mapping):
        error = ToolError(
            str(error_payload.get("code") or "durable_replay_error"),
            str(error_payload.get("message") or "durably replayed tool error"),
            error_payload.get("details") or {},
        )
    return ToolResult(
        call_id=str(payload.get("call_id") or "durable-replay"),
        canonical_name=str(payload.get("canonical_name") or "unknown"),
        status=ToolResultStatus(str(payload.get("status") or ToolResultStatus.INCOMPLETE.value)),
        data=payload.get("data") or {},
        error=error,
        attempted_effects=tuple(_effect_from_dict(item) for item in payload.get("attempted_effects") or ()),
        observed_effects=tuple(_effect_from_dict(item) for item in payload.get("observed_effects") or ()),
        committed_effects=tuple(_effect_from_dict(item) for item in payload.get("committed_effects") or ()),
        unknown_effects=tuple(_effect_from_dict(item) for item in payload.get("unknown_effects") or ()),
        artifacts=tuple(payload.get("artifacts") or ()),
        truncation=payload.get("truncation"),
        continuation=payload.get("continuation"),
        backend=str(payload.get("backend") or "runtime_v3_durable_replay"),
        duration_ms=float(payload.get("duration_ms") or 0.0),
    )


def begin_tool_effect(
    call: NormalizedToolCall,
    context: AgentExecutionContext,
    effects: Iterable[Effect],
    *,
    ledger: DurableRunLedger | None = None,
    idempotency_scope: str | None = None,
) -> DurableEffectHandle:
    target = ledger or get_runtime_ledger()
    _ensure_run(target, context)
    effect_class, retry_policy = _classify_effects(effects)
    # An approval-scoped call has a stronger retry oracle than a generic
    # process/write call: Runtime V2 records whether execution crossed the
    # effect boundary. A FAILED durable result is therefore retryable only for
    # this exact approval scope; once V2 reports any observed/unknown effect we
    # persist UNKNOWN instead and the ledger refuses replay.
    if idempotency_scope:
        retry_policy = RetryPolicy.SAFE
    recorded_arguments = {
        "call_id": call.call_id,
        "candidate_id": call.candidate_id,
        "provider_round": call.provider_round,
        "tool": call.canonical_name,
        "arguments": redact_sensitive(dict(call.arguments)),
        "arguments_sha256": call.arguments_digest,
        "authority_revision": call.authority_revision,
        "workspace_revision": call.workspace_revision,
        "tool_contract_revision": call.tool_contract_revision,
        "idempotency_scope": idempotency_scope or "default",
    }
    lease = target.begin_effect(
        run_id=context.run_id,
        tool_name=call.canonical_name,
        arguments=recorded_arguments,
        effect_class=effect_class,
        retry_policy=retry_policy,
        idempotency_key=(
            f"{call.candidate_id}:{call.call_id}:{idempotency_scope}"
            if idempotency_scope
            else f"{call.candidate_id}:{call.call_id}"
        ),
    )
    replay = None
    if not lease.should_execute and lease.status is EffectStatus.COMMITTED and lease.cached_result:
        replay = tool_result_from_dict(lease.cached_result).bind_call(call)
    target.append_event(
        context.run_id,
        "tool_effect_lease",
        {
            "call_id": call.call_id,
            "canonical_name": call.canonical_name,
            "effect_id": lease.effect_id,
            "effect_class": effect_class.value,
            "retry_policy": retry_policy.value,
            "status": lease.status.value,
            "should_execute": lease.should_execute,
            "blocked_reason": lease.reason,
        },
    )
    return DurableEffectHandle(
        ledger=target,
        run_id=context.run_id,
        effect_id=lease.effect_id,
        effect_class=effect_class,
        retry_policy=retry_policy,
        should_execute=lease.should_execute,
        replay_result=replay,
        blocked_reason=lease.reason,
    )


def duplicate_effect_result(call: NormalizedToolCall, handle: DurableEffectHandle) -> ToolResult:
    if handle.replay_result is not None:
        return handle.replay_result
    return ToolResult(
        call_id=call.call_id,
        canonical_name=call.canonical_name,
        status=ToolResultStatus.INCOMPLETE,
        data={
            "effect_id": handle.effect_id,
            "effect_class": handle.effect_class.value,
            "retry_policy": handle.retry_policy.value,
            "reconciliation_required": True,
        },
        error=ToolError(
            "effect_reconciliation_required",
            "This exact tool invocation already started but has no safely replayable committed result.",
        ),
        backend="runtime_v3_effect_bridge",
    )


def _durable_result_payload(handle: DurableEffectHandle, result: ToolResult) -> dict[str, Any]:
    payload = result.as_dict()
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    if len(raw.encode("utf-8")) <= _RESULT_INLINE_LIMIT:
        return payload
    observation = handle.ledger.store_observation(handle.run_id, "tool_result", raw)
    compact = dict(payload)
    compact["data"] = {
        "durable_observation": observation,
        "summary": result.model_text()[:4000],
        "full_result_inline": False,
    }
    compact["artifacts"] = []
    return compact


def finish_tool_effect(handle: DurableEffectHandle, result: ToolResult) -> None:
    if not handle.should_execute:
        return
    payload = _durable_result_payload(handle, result)
    uncertain_status = result.status in {
        ToolResultStatus.TIMED_OUT,
        ToolResultStatus.CANCELLED,
        ToolResultStatus.ERROR,
        ToolResultStatus.INCOMPLETE,
    }
    # Runtime V2's effect evidence is authoritative for whether a failed call
    # may have crossed into an effect. A pre-effect failure has no observed,
    # committed, or unknown effects and can be durably FAILED; any partial or
    # ambiguous effectful failure remains UNKNOWN and is never auto-retried.
    has_effect_evidence = bool(
        result.observed_effects
        or result.committed_effects
        or result.unknown_effects
    )
    unknown = bool(result.unknown_effects) or (
        handle.effect_class is not EffectClass.READ
        and uncertain_status
        and has_effect_evidence
    )
    if unknown:
        handle.ledger.mark_effect_unknown(
            handle.effect_id,
            {"tool_result": payload, "reason": "effect_outcome_not_proven"},
        )
        terminal_status = EffectStatus.UNKNOWN
    elif result.status is ToolResultStatus.SUCCESS:
        handle.ledger.finish_effect(handle.effect_id, payload)
        terminal_status = EffectStatus.COMMITTED
    else:
        handle.ledger.fail_effect(handle.effect_id, {"tool_result": payload})
        terminal_status = EffectStatus.FAILED
    handle.ledger.append_event(
        handle.run_id,
        "tool_effect_terminal",
        {
            "effect_id": handle.effect_id,
            "call_id": result.call_id,
            "canonical_name": result.canonical_name,
            "status": terminal_status.value,
            "tool_status": result.status.value,
            "observed_effect_count": len(result.observed_effects),
            "unknown_effect_count": len(result.unknown_effects),
            "committed_effect_count": len(result.committed_effects),
        },
    )
