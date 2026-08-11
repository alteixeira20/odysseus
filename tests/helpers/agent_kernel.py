"""Test-only authority harness for the internal legacy Agent kernel.

Production callers must enter AgentRunner. Kernel characterization tests use
this helper to supply the explicit Runtime V2 execution context that the
production runner would have prepared before invoking the kernel.
"""

from __future__ import annotations

import inspect
import uuid
from typing import Any, AsyncGenerator

from src import agent_loop
from src.agent.runtime_v2.authority import prepare_execution_context
from src.agent.runtime_v2.contracts import RunBudgets
from src.agent.tools.bootstrap import TOOL_REGISTRY
from src.execution_policy import normalize_execution_mode


def _default_kernel_argument(name: str) -> Any:
    parameter = inspect.signature(
        agent_loop._legacy_stream_agent_kernel
    ).parameters[name]
    if parameter.default is inspect.Parameter.empty:
        raise ValueError(f"kernel argument {name!r} has no default")
    return parameter.default


def prepare_legacy_kernel_context(kwargs: dict[str, Any]):
    """Recreate the removed compatibility authority only for tests."""

    if kwargs.get("execution_context") is not None:
        return kwargs["execution_context"]

    owner = kwargs.get("owner")
    plan_mode = bool(kwargs.get("plan_mode", False))
    disabled = set(kwargs.get("disabled_tools") or ())
    tool_policy = kwargs.get("tool_policy")
    if tool_policy is not None:
        all_disabled = getattr(tool_policy, "all_disabled_names", None)
        if callable(all_disabled):
            disabled.update(all_disabled())
    disabled.update(agent_loop.blocked_tools_for_owner(owner))
    if plan_mode:
        disabled.update(agent_loop.plan_mode_disabled_tools())

    shell_enabled = kwargs.get("shell_enabled")
    if shell_enabled is None:
        shell_enabled = _default_kernel_argument("shell_enabled")
    requested_mode = normalize_execution_mode(
        shell_enabled,
        shell_enabled=shell_enabled,
    )

    max_rounds = kwargs.get("max_rounds")
    if max_rounds is None:
        max_rounds = _default_kernel_argument("max_rounds")
    max_rounds = int(max_rounds)

    max_tool_calls = kwargs.get("max_tool_calls")
    if max_tool_calls is None:
        max_tool_calls = _default_kernel_argument("max_tool_calls")
    effective_tool_calls = (
        int(max_tool_calls)
        if max_tool_calls and int(max_tool_calls) > 0
        else 256
    )

    context, _ = prepare_execution_context(
        owner_id=owner,
        session_id=str(kwargs.get("session_id") or "compatibility-run"),
        requested_mode=requested_mode,
        selected_workspace=kwargs.get("workspace"),
        budgets=RunBudgets(
            max_rounds=max_rounds,
            max_tool_calls=effective_tool_calls,
            max_provider_requests=min(max(max_rounds * 3, 1), 128),
        ),
        tool_catalog_revision=TOOL_REGISTRY.revision,
        plan_mode=plan_mode,
        sandbox_default=agent_loop.get_setting(
            "agent_sandbox_default_root", None
        ),
        host_default=agent_loop.get_setting("agent_host_default_root", None),
        run_id=f"kernel-test-{uuid.uuid4().hex}",
        disabled_tools=TOOL_REGISTRY.canonicalize_names(disabled, None),
    )
    return context


async def stream_legacy_kernel(
    *args: Any,
    **kwargs: Any,
) -> AsyncGenerator[str, None]:
    """Invoke the internal kernel with explicit test-owned authority."""

    kwargs = dict(kwargs)
    if kwargs.get("execution_context") is None:
        kwargs["execution_context"] = prepare_legacy_kernel_context(kwargs)
    async for event in agent_loop._legacy_stream_agent_kernel(*args, **kwargs):
        yield event


__all__ = ["prepare_legacy_kernel_context", "stream_legacy_kernel"]
