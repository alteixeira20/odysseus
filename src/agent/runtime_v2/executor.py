"""The one registry/policy/handler path for normalized Runtime V2 calls."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any, Optional

from src.agent.tools.bootstrap import TOOL_REGISTRY

from .contracts import (
    AgentExecutionContext,
    ApprovalDecision,
    NormalizedToolCall,
    ToolError,
    ToolResult,
    ToolResultStatus,
)
from .effect_policy import EFFECT_POLICY
from .ownership import OwnershipError, RUN_OWNERSHIP
from .approvals import EFFECT_APPROVALS, EffectApprovalError


def _tool_error(
    call: NormalizedToolCall,
    *,
    status: ToolResultStatus,
    code: str,
    message: str,
    data: Optional[dict[str, Any]] = None,
    duration_ms: float = 0.0,
    attempted_effects=(),
    observed_effects=(),
    unknown_effects=(),
) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        canonical_name=call.canonical_name,
        status=status,
        data=data or {},
        error=ToolError(code, message),
        attempted_effects=tuple(attempted_effects),
        observed_effects=tuple(observed_effects),
        unknown_effects=tuple(unknown_effects),
        backend="runtime_v2_executor",
        duration_ms=duration_ms,
    )


async def execute_normalized_tool_call(
    call: NormalizedToolCall,
    execution_context: AgentExecutionContext,
    *,
    progress_cb=None,
    approval_id: Optional[str] = None,
) -> ToolResult:
    """Resolve -> validate -> effects -> policy -> handler -> result validation."""

    started = time.perf_counter()
    approval_claimed = False
    effect_started = False
    effects = ()

    def effect_boundary() -> None:
        nonlocal effect_started
        if effect_started:
            return
        if approval_id:
            EFFECT_APPROVALS.mark_executing(approval_id)
        effect_started = True
    try:
        execution_context.cancellation_token.raise_if_cancelled()
        RUN_OWNERSHIP.validate_call_identity(execution_context, call)
    except asyncio.CancelledError:
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.CANCELLED,
            code="run_cancelled",
            message="run was cancelled before tool execution",
        )
    except OwnershipError as exc:
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="stale_or_unauthorized_call",
            message=str(exc),
        )
    except (OSError, RuntimeError) as exc:
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="approval_snapshot_unavailable",
            message=str(exc),
        )
    definition = TOOL_REGISTRY.resolve(call.canonical_name, execution_context)
    if definition is None or not definition.runtime_v2:
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="canonical_tool_unavailable",
            message=f"canonical tool is unavailable: {call.canonical_name}",
        )
    if definition.name != call.canonical_name:
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="noncanonical_dispatch",
            message="executor accepts only canonical normalized tool names",
        )
    if definition.name in execution_context.authority_grant.disabled_tools:
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="tool_disabled",
            message=f"{definition.name} is explicitly disabled for this run",
        )
    if not definition.exposed(execution_context):
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="tool_not_exposed",
            message=f"{definition.name} is not exposed for this authority snapshot",
        )
    try:
        RUN_OWNERSHIP.validate_effective_tool(
            execution_context,
            definition.name,
        )
    except OwnershipError as exc:
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="tool_not_available",
            message=str(exc),
        )
    if call.normalization_error:
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.ERROR,
            code="argument_validation_error",
            message=call.normalization_error,
        )
    try:
        arguments = definition.validate_arguments(call.arguments)
    except ValueError as exc:
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.ERROR,
            code="argument_validation_error",
            message=str(exc),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    for capability in definition.required_capabilities:
        if not execution_context.authority_grant.allows(capability):
            if approval_id:
                EFFECT_APPROVALS.invalidate(approval_id)
            return _tool_error(
                call,
                status=ToolResultStatus.DENIED,
                code="capability_denied",
                message=f"authority does not grant {capability.value}",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
    try:
        effects = definition.resolve_effects(arguments, execution_context)
    except (ValueError, RuntimeError) as exc:
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="effect_resolution_error",
            message=str(exc),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    outcome = EFFECT_POLICY.evaluate(
        execution_context,
        effects,
        approval_policy=definition.approval_policy,
    )
    effect_data = {
        "effects": [
            {
                "kind": effect.kind,
                "target": effect.target,
                "key": effect.key,
                "capability": effect.capability.value,
                "consequential": effect.consequential,
                "destructive": effect.destructive,
                "opaque": effect.opaque,
            }
            for effect in effects
        ]
    }
    if outcome.decision is ApprovalDecision.DENY:
        if approval_id:
            EFFECT_APPROVALS.invalidate(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="effect_denied",
            message=outcome.reason,
            data=effect_data,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    if outcome.decision is ApprovalDecision.REQUIRE_APPROVAL:
        if approval_id:
            try:
                EFFECT_APPROVALS.claim(
                    approval_id,
                    call=call,
                    context=execution_context,
                    effects=effects,
                )
                approval_claimed = True
            except EffectApprovalError as exc:
                return _tool_error(
                    call,
                    status=ToolResultStatus.DENIED,
                    code=exc.code,
                    message=str(exc),
                    data=effect_data,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
        else:
            try:
                request = EFFECT_APPROVALS.request(
                    call=call,
                    context=execution_context,
                    effects=effects,
                )
            except EffectApprovalError as exc:
                return _tool_error(
                    call,
                    status=ToolResultStatus.DENIED,
                    code="approval_snapshot_unavailable",
                    message=str(exc),
                    data=effect_data,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
            effect_data["approval"] = {
                "approval_id": request.approval_id,
                "expires_at": request.expires_at,
                "call_id": request.call_id,
                "canonical_name": request.canonical_name,
                "effects_digest": request.effects_digest,
                "one_use": True,
            }
            return _tool_error(
                call,
                status=ToolResultStatus.APPROVAL_REQUIRED,
                code="effect_approval_required",
                message=outcome.reason,
                data=effect_data,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
    if execution_context.cancellation_token.cancelled:
        if approval_claimed:
            EFFECT_APPROVALS.fail_before_effect(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.CANCELLED,
            code="run_cancelled",
            message="run was cancelled before tool execution",
            duration_ms=(time.perf_counter() - started) * 1000,
            attempted_effects=effects if approval_claimed else (),
        )
    try:
        handler_kwargs: dict[str, Any] = {}
        if definition.supports_progress:
            handler_kwargs.update(
                progress_cb=progress_cb,
                invocation_id=call.call_id,
            )
        if definition.name in {
            "patch_workspace",
            "run_sandbox_command",
            "run_host_command",
            "run_python",
        }:
            handler_kwargs["effect_started_cb"] = effect_boundary
        if definition.name in {
            "run_sandbox_command",
            "run_host_command",
            "run_python",
        }:
            handler_kwargs["expected_process_identity"] = next(
                (
                    str(effect.metadata.get("process_identity"))
                    for effect in effects
                    if effect.metadata.get("process_identity")
                ),
                None,
            )
        handler_call = definition.handler(
            arguments,
            execution_context,
            **handler_kwargs,
        )
        raw_result = await asyncio.wait_for(
            handler_call,
            timeout=definition.timeout_seconds,
        )
    except asyncio.TimeoutError:
        started_process_effects = tuple(
            effect
            for effect in effects
            if effect.kind.startswith("process.execute.")
        )
        if approval_claimed:
            if effect_started:
                EFFECT_APPROVALS.fail_after_unknown_effect(approval_id)
            else:
                EFFECT_APPROVALS.fail_before_effect(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.TIMED_OUT,
            code="tool_timeout",
            message=f"{definition.name} exceeded its {definition.timeout_seconds:g}s timeout",
            duration_ms=(time.perf_counter() - started) * 1000,
            attempted_effects=effects,
            observed_effects=started_process_effects if effect_started else (),
            unknown_effects=effects if effect_started else (),
        )
    except asyncio.CancelledError:
        started_process_effects = tuple(
            effect
            for effect in effects
            if effect.kind.startswith("process.execute.")
        )
        if approval_claimed:
            if effect_started:
                EFFECT_APPROVALS.fail_after_unknown_effect(approval_id)
            else:
                EFFECT_APPROVALS.fail_before_effect(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.CANCELLED,
            code="run_cancelled",
            message="run was cancelled during tool execution",
            duration_ms=(time.perf_counter() - started) * 1000,
            attempted_effects=effects,
            observed_effects=started_process_effects if effect_started else (),
            unknown_effects=effects if effect_started else (),
        )
    except Exception as exc:
        started_process_effects = tuple(
            effect
            for effect in effects
            if effect.kind.startswith("process.execute.")
        )
        if approval_claimed:
            if effect_started:
                EFFECT_APPROVALS.fail_after_unknown_effect(approval_id)
            else:
                EFFECT_APPROVALS.fail_before_effect(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.ERROR,
            code="handler_exception",
            message=f"{definition.name} failed: {exc}",
            duration_ms=(time.perf_counter() - started) * 1000,
            attempted_effects=effects,
            observed_effects=started_process_effects if effect_started else (),
            unknown_effects=effects if effect_started else (),
        )
    try:
        bound_result = raw_result.bind_call(call)
        process_tool = definition.name in {
            "run_sandbox_command",
            "run_host_command",
            "run_python",
        }
        process_started = bool(effect_started or bound_result.data.get("effect_started"))
        attempted_effects = tuple(effects)
        observed_effects = tuple(bound_result.observed_effects)
        committed_effects = tuple(bound_result.committed_effects)
        unknown_effects = tuple(bound_result.unknown_effects)
        if process_tool:
            process_effects = tuple(
                effect for effect in effects if effect.kind.startswith("process.execute.")
            )
            nonprocess_effects = tuple(
                effect for effect in effects if not effect.kind.startswith("process.execute.")
            )
            if process_started:
                observed_effects = process_effects
                if bound_result.data.get("workspace_mutated"):
                    observed_effects += tuple(
                        effect
                        for effect in nonprocess_effects
                        if effect.kind == "filesystem.process_write"
                    )
                unknown_effects = tuple(
                    effect
                    for effect in nonprocess_effects
                    if effect not in observed_effects
                )
                if (
                    bound_result.status is not ToolResultStatus.SUCCESS
                    or bound_result.data.get("background")
                ):
                    # The start is observed, while the final behavior of an
                    # opaque failed/interrupted/detached process is not.
                    unknown_effects = tuple(effects)
            # Opaque process predictions are never promoted to committed
            # workspace/external effects merely because a process was invoked.
            committed_effects = ()
        elif definition.name == "patch_workspace":
            if bound_result.status is ToolResultStatus.SUCCESS:
                observed_effects = tuple(effects)
                if not bool(arguments.get("dry_run")):
                    committed_effects = tuple(effects)
            elif effect_started:
                unknown_effects = tuple(effects)
        elif bound_result.status is ToolResultStatus.SUCCESS:
            observed_effects = tuple(effects)

        bound_result = replace(
            bound_result,
            attempted_effects=attempted_effects,
            observed_effects=observed_effects,
            committed_effects=committed_effects,
            unknown_effects=unknown_effects,
            duration_ms=max(
                bound_result.duration_ms,
                (time.perf_counter() - started) * 1000,
            ),
        )
        validated_result = definition.validate_result(bound_result)
        if approval_claimed:
            if (
                bound_result.status is ToolResultStatus.SUCCESS
                and not bound_result.data.get("background")
            ):
                EFFECT_APPROVALS.complete(
                    approval_id,
                    committed_effects=bool(validated_result.committed_effects),
                )
            elif effect_started:
                EFFECT_APPROVALS.fail_after_unknown_effect(approval_id)
            else:
                EFFECT_APPROVALS.fail_before_effect(approval_id)
        return validated_result
    except (AttributeError, TypeError, ValueError) as exc:
        if approval_claimed:
            if effect_started:
                EFFECT_APPROVALS.fail_after_unknown_effect(approval_id)
            else:
                EFFECT_APPROVALS.fail_before_effect(approval_id)
        return _tool_error(
            call,
            status=ToolResultStatus.ERROR,
            code="result_validation_error",
            message=f"{definition.name} returned a malformed result: {exc}",
            duration_ms=(time.perf_counter() - started) * 1000,
            attempted_effects=effects,
            unknown_effects=effects if effect_started else (),
        )
