"""Canonical typed control plane for Agent execution.

The public Agent API enters here.  Runtime V3 durability and Runtime V2
execution authority are implementation services owned by :class:`AgentRunner`,
not independent orchestration layers.

``AgentLoopCompatibilityBackend`` is the temporary strangler seam around the
remaining legacy loop.  No caller outside this module should project a typed
``AgentRunRequest`` back into the legacy keyword-argument surface.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, AsyncGenerator, Callable, Protocol

from src.execution_policy import normalize_execution_mode
from src.settings import get_setting
from src.tool_security import blocked_tools_for_owner, plan_mode_disabled_tools

from .contracts import AgentRunRequest
from .runtime_v2.authority import prepare_execution_context_async
from .runtime_v2.contracts import RunBudgets
from .runtime_v3.config import load_runtime_v3_limits
from .runtime_v3.orchestrator import stream_with_durable_runtime
from .tools.bootstrap import TOOL_REGISTRY


class AgentBackend(Protocol):
    """Execution backend consumed by the canonical runner."""

    def stream(self, request: AgentRunRequest) -> Any:
        """Return an async iterator of the current SSE compatibility stream."""


class AgentLoopCompatibilityBackend:
    """Temporary adapter around ``src.agent_loop``.

    This is deliberately the *only* typed-to-legacy projection point.  The
    legacy module stays import-lazy so importing ``src.agent`` contracts does
    not bootstrap the historical orchestration module.
    """

    @staticmethod
    def arguments(request: AgentRunRequest) -> dict[str, Any]:
        effective = load_runtime_v3_limits().normalize_request(
            max_rounds=request.limits.max_rounds,
            max_tool_calls=request.limits.max_tool_calls,
        )
        return {
            "endpoint_url": request.endpoint_url,
            "model": request.model,
            "messages": request.messages,
            "headers": request.model_options.headers,
            "temperature": request.model_options.temperature,
            "max_tokens": request.limits.max_tokens,
            "prompt_type": request.model_options.prompt_type,
            "max_rounds": effective.max_rounds,
            "max_tool_calls": effective.max_tool_calls,
            "context_length": request.limits.context_length,
            "active_document": request.contexts.active_document,
            "active_email": request.contexts.active_email,
            "session_id": request.session_id,
            "disabled_tools": (
                set(request.policy.disabled_tools)
                if request.policy.disabled_tools is not None
                else None
            ),
            "owner": request.owner,
            "relevant_tools": (
                set(request.policy.relevant_tools)
                if request.policy.relevant_tools is not None
                else None
            ),
            "fallbacks": (
                list(request.model_options.fallbacks)
                if request.model_options.fallbacks is not None
                else None
            ),
            "plan_mode": request.plan_mode,
            "approved_plan": request.approved_plan,
            "tool_policy": request.policy.policy,
            "workspace": request.contexts.workspace,
            "forced_tools": (
                set(request.policy.forced_tools)
                if request.policy.forced_tools is not None
                else None
            ),
            "uploaded_files": (
                list(request.contexts.uploaded_files)
                if request.contexts.uploaded_files is not None
                else None
            ),
            "workload": request.workload,
            "_is_teacher_run": request.is_teacher_run,
            "shell_enabled": (
                request.policy.execution_mode
                if request.policy.execution_mode is not None
                else request.policy.shell_enabled
            ),
            "execution_context": request.execution_context,
        }

    def stream(self, request: AgentRunRequest):
        from src.agent_loop import stream_agent_loop as legacy_stream_agent_loop

        return legacy_stream_agent_loop(**self.arguments(request))


@dataclass(frozen=True)
class PreparedAgentRun:
    """Immutable result of the runner's authority-preparation phase."""

    request: AgentRunRequest
    authority_reason: str = "provided"
    prepared_here: bool = False


class AgentRunner:
    """Own one Agent run from typed request to durable terminal stream.

    The runner is intentionally small while the strangler migration is in
    progress, but its ownership boundary is final: request normalization,
    execution authority, durable lifecycle and backend invocation all enter
    through this object.  Future provider/context/supervisor extraction should
    replace the compatibility backend from the inside rather than adding a new
    runtime layer around it.
    """

    def __init__(
        self,
        *,
        backend: AgentBackend | None = None,
        durable_stream: Callable[..., Any] = stream_with_durable_runtime,
        prepare_execution_context: Callable[..., Any] = prepare_execution_context_async,
    ) -> None:
        self.backend: AgentBackend = backend or AgentLoopCompatibilityBackend()
        self._durable_stream = durable_stream
        self._prepare_execution_context = prepare_execution_context

    @staticmethod
    def _compatibility_disabled_tools(request: AgentRunRequest) -> frozenset[str]:
        disabled = set(request.policy.disabled_tools or ())
        policy = request.policy.policy
        if policy is not None:
            all_disabled = getattr(policy, "all_disabled_names", None)
            if callable(all_disabled):
                disabled.update(all_disabled())
        disabled.update(blocked_tools_for_owner(request.owner))
        if request.plan_mode:
            disabled.update(plan_mode_disabled_tools())
        return TOOL_REGISTRY.canonicalize_names(disabled, None)

    @staticmethod
    def _run_budgets(request: AgentRunRequest) -> RunBudgets:
        effective = load_runtime_v3_limits().normalize_request(
            max_rounds=request.limits.max_rounds,
            max_tool_calls=request.limits.max_tool_calls,
        )
        return RunBudgets(
            max_rounds=effective.max_rounds,
            max_tool_calls=(
                effective.max_tool_calls
                if effective.max_tool_calls and effective.max_tool_calls > 0
                else 256
            ),
            max_provider_requests=min(max(effective.max_rounds * 3, 1), 128),
        )

    async def prepare(self, request: AgentRunRequest) -> PreparedAgentRun:
        """Attach immutable Runtime V2 authority exactly once.

        HTTP callers may already provide a server-prepared context (for
        compare-and-set turn activation and one-use host authorization).  That
        object is preserved by identity.  Compatibility/direct callers are
        prepared here so the legacy backend no longer owns production control
        plane construction.
        """

        if request.execution_context is not None:
            return PreparedAgentRun(request=request)

        requested = (
            request.policy.execution_mode
            if request.policy.execution_mode is not None
            else request.policy.shell_enabled
        )
        requested_mode = normalize_execution_mode(
            requested,
            shell_enabled=request.policy.shell_enabled,
        )
        context, reason = await self._prepare_execution_context(
            owner_id=request.owner,
            session_id=str(request.session_id or "compatibility-run"),
            requested_mode=requested_mode,
            selected_workspace=request.contexts.workspace,
            budgets=self._run_budgets(request),
            tool_catalog_revision=TOOL_REGISTRY.revision,
            plan_mode=request.plan_mode,
            sandbox_default=get_setting("agent_sandbox_default_root", None),
            host_default=get_setting("agent_host_default_root", None),
            disabled_tools=self._compatibility_disabled_tools(request),
        )
        return PreparedAgentRun(
            request=replace(request, execution_context=context),
            authority_reason=reason,
            prepared_here=True,
        )

    async def stream(self, request: AgentRunRequest) -> AsyncGenerator[str, None]:
        prepared = await self.prepare(request)
        typed_request = prepared.request
        async for event in self._durable_stream(
            typed_request,
            lambda: self.backend.stream(typed_request),
        ):
            yield event


DEFAULT_AGENT_RUNNER = AgentRunner()


__all__ = [
    "AgentBackend",
    "AgentLoopCompatibilityBackend",
    "AgentRunner",
    "DEFAULT_AGENT_RUNNER",
    "PreparedAgentRun",
]
