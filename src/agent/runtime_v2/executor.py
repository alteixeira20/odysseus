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


def _tool_error(
    call: NormalizedToolCall,
    *,
    status: ToolResultStatus,
    code: str,
    message: str,
    data: Optional[dict[str, Any]] = None,
    duration_ms: float = 0.0,
) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        canonical_name=call.canonical_name,
        status=status,
        data=data or {},
        error=ToolError(code, message),
        backend="runtime_v2_executor",
        duration_ms=duration_ms,
    )


async def execute_normalized_tool_call(
    call: NormalizedToolCall,
    execution_context: AgentExecutionContext,
    *,
    progress_cb=None,
) -> ToolResult:
    """Resolve -> validate -> effects -> policy -> handler -> result validation."""

    started = time.perf_counter()
    execution_context.cancellation_token.raise_if_cancelled()
    definition = TOOL_REGISTRY.resolve(call.canonical_name, execution_context)
    if definition is None or not definition.runtime_v2:
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="canonical_tool_unavailable",
            message=f"canonical tool is unavailable: {call.canonical_name}",
        )
    if definition.name != call.canonical_name:
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="noncanonical_dispatch",
            message="executor accepts only canonical normalized tool names",
        )
    if definition.name in execution_context.authority_grant.disabled_tools:
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="tool_disabled",
            message=f"{definition.name} is explicitly disabled for this run",
        )
    if not definition.exposed(execution_context):
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="tool_not_exposed",
            message=f"{definition.name} is not exposed for this authority snapshot",
        )
    if call.normalization_error:
        return _tool_error(
            call,
            status=ToolResultStatus.ERROR,
            code="argument_validation_error",
            message=call.normalization_error,
        )
    try:
        arguments = definition.validate_arguments(call.arguments)
    except ValueError as exc:
        return _tool_error(
            call,
            status=ToolResultStatus.ERROR,
            code="argument_validation_error",
            message=str(exc),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    for capability in definition.required_capabilities:
        if not execution_context.authority_grant.allows(capability):
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
        return _tool_error(
            call,
            status=ToolResultStatus.DENIED,
            code="effect_denied",
            message=outcome.reason,
            data=effect_data,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    if outcome.decision is ApprovalDecision.REQUIRE_APPROVAL:
        return _tool_error(
            call,
            status=ToolResultStatus.APPROVAL_REQUIRED,
            code="effect_approval_required",
            message=outcome.reason,
            data=effect_data,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    if execution_context.cancellation_token.cancelled:
        return _tool_error(
            call,
            status=ToolResultStatus.CANCELLED,
            code="run_cancelled",
            message="run was cancelled before tool execution",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    try:
        handler_call = (
            definition.handler(
                arguments,
                execution_context,
                progress_cb=progress_cb,
                invocation_id=call.call_id,
            )
            if definition.supports_progress
            else definition.handler(arguments, execution_context)
        )
        raw_result = await asyncio.wait_for(
            handler_call,
            timeout=definition.timeout_seconds,
        )
    except asyncio.TimeoutError:
        return _tool_error(
            call,
            status=ToolResultStatus.TIMED_OUT,
            code="tool_timeout",
            message=f"{definition.name} exceeded its {definition.timeout_seconds:g}s timeout",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    except asyncio.CancelledError:
        return _tool_error(
            call,
            status=ToolResultStatus.CANCELLED,
            code="run_cancelled",
            message="run was cancelled during tool execution",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:
        return _tool_error(
            call,
            status=ToolResultStatus.ERROR,
            code="handler_exception",
            message=f"{definition.name} failed: {exc}",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    try:
        bound_result = raw_result.bind_call(call)
        effects_committed = (
            tuple(effects)
            if (
                bound_result.status is ToolResultStatus.SUCCESS
                and not (
                    definition.name == "patch_workspace"
                    and bool(arguments.get("dry_run"))
                )
                or definition.name
                in {"run_sandbox_command", "run_host_command", "run_python"}
            )
            else tuple(bound_result.committed_effects)
        )
        bound_result = replace(
            bound_result,
            committed_effects=effects_committed,
            duration_ms=max(
                bound_result.duration_ms,
                (time.perf_counter() - started) * 1000,
            ),
        )
        return definition.validate_result(bound_result)
    except (AttributeError, TypeError, ValueError) as exc:
        return _tool_error(
            call,
            status=ToolResultStatus.ERROR,
            code="result_validation_error",
            message=f"{definition.name} returned a malformed result: {exc}",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
