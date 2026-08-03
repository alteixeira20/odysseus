from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Iterable

from src.agent.runtime_v2.contracts import (
    AgentExecutionContext,
    ApprovalDecision,
    Effect,
    NormalizedToolCall,
    ToolError,
    ToolResult,
    ToolResultStatus,
)
from src.agent.runtime_v2.effect_policy import EFFECT_POLICY
from src.agent.tools.bootstrap import TOOL_REGISTRY

from .effect_bridge import (
    DurableEffectHandle,
    begin_tool_effect,
    duplicate_effect_result,
    finish_tool_effect,
)


Implementation = Callable[..., Awaitable[ToolResult]]


def _preflight_effects(
    call: NormalizedToolCall,
    context: AgentExecutionContext,
    *,
    approval_id: str | None,
) -> tuple[Effect, ...] | None:
    """Resolve effects without crossing an execution boundary.

    Returning ``None`` means the authoritative implementation must handle a
    denial or approval request and therefore cannot execute a handler.
    Unexpected preflight failures are surfaced by the wrapper instead of
    bypassing durability.
    """
    definition = TOOL_REGISTRY.resolve(call.canonical_name, context)
    if definition is None or not definition.runtime_v2:
        return None
    if definition.name != call.canonical_name:
        return None
    if definition.name in context.authority_grant.disabled_tools:
        return None
    if not definition.exposed(context) or call.normalization_error:
        return None
    arguments = definition.validate_arguments(call.arguments)
    for capability in definition.required_capabilities:
        if not context.authority_grant.allows(capability):
            return None
    effects = tuple(definition.resolve_effects(arguments, context))
    outcome = EFFECT_POLICY.evaluate(
        context,
        effects,
        approval_policy=definition.approval_policy,
    )
    if outcome.decision is ApprovalDecision.DENY:
        return None
    if outcome.decision is ApprovalDecision.REQUIRE_APPROVAL and not approval_id:
        return None
    return effects


def _bridge_error(
    call: NormalizedToolCall,
    *,
    code: str,
    message: str,
    effects: Iterable[Effect] = (),
    unknown: bool = False,
    data: dict[str, Any] | None = None,
) -> ToolResult:
    effect_tuple = tuple(effects)
    return ToolResult(
        call_id=call.call_id,
        canonical_name=call.canonical_name,
        status=ToolResultStatus.INCOMPLETE,
        data=data or {},
        error=ToolError(code, message),
        attempted_effects=effect_tuple,
        unknown_effects=effect_tuple if unknown else (),
        backend="runtime_v3_executor_bridge",
    )


async def execute_with_durable_effects(
    call: NormalizedToolCall,
    context: AgentExecutionContext,
    *,
    implementation: Implementation,
    progress_cb=None,
    approval_id: str | None = None,
) -> ToolResult:
    try:
        effects = _preflight_effects(call, context, approval_id=approval_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _bridge_error(
            call,
            code="durable_effect_preflight_failed",
            message=f"Tool execution was blocked because durable effect preflight failed: {exc}",
        )

    if effects is None:
        return await implementation(
            call,
            context,
            progress_cb=progress_cb,
            approval_id=approval_id,
        )

    handle: DurableEffectHandle
    try:
        handle = begin_tool_effect(call, context, effects)
    except (OSError, RuntimeError, ValueError) as exc:
        return _bridge_error(
            call,
            code="durable_effect_ledger_unavailable",
            message=f"Tool execution was blocked because its durable effect lease could not be recorded: {exc}",
            effects=effects,
        )

    if not handle.should_execute:
        return duplicate_effect_result(call, handle)

    try:
        result = await implementation(
            call,
            context,
            progress_cb=progress_cb,
            approval_id=approval_id,
        )
    except asyncio.CancelledError:
        try:
            handle.ledger.mark_effect_unknown(
                handle.effect_id,
                {"reason": "executor_task_cancelled_before_typed_result"},
            )
        finally:
            raise
    except BaseException as exc:
        try:
            handle.ledger.mark_effect_unknown(
                handle.effect_id,
                {"reason": "executor_raised_before_typed_result", "type": type(exc).__name__},
            )
        finally:
            raise

    try:
        finish_tool_effect(handle, result)
    except (OSError, RuntimeError, ValueError) as exc:
        return _bridge_error(
            call,
            code="durable_effect_finalization_failed",
            message=(
                "The tool returned, but the durable effect outcome could not be finalized. "
                "Treat the effect as unknown until reconciled."
            ),
            effects=effects,
            unknown=True,
            data={
                "effect_id": handle.effect_id,
                "original_tool_result": result.as_dict(),
                "ledger_error": str(exc)[:1000],
                "reconciliation_required": True,
            },
        )
    return result
