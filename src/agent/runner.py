"""Canonical typed control plane for Agent execution.

The public Agent API enters here. Runtime V3 durability and Runtime V2
execution authority are implementation services owned by :class:`AgentRunner`,
not independent orchestration layers.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, AsyncGenerator, Callable, Protocol

from src.execution_policy import normalize_execution_mode
from src.settings import get_setting
from src.tool_security import blocked_tools_for_owner, plan_mode_disabled_tools

from .contracts import AgentAuthorityRequest, AgentRunRequest
from .runtime_v2.authority import prepare_execution_context_async
from .runtime_v2.contracts import AgentExecutionContext, Capability, RunBudgets
from .runtime_v3.config import load_runtime_v3_limits
from .runtime_v3.lifecycle import DurableRunLifecycle
from .tools.bootstrap import TOOL_REGISTRY


class AgentBackend(Protocol):
    def stream(self, request: AgentRunRequest) -> Any:
        """Return an async iterator of the current SSE compatibility stream."""


class AgentLoopCompatibilityBackend:
    """Temporary adapter to the internal execution kernel.

    Public callers never enter this backend directly. ``src.agent_loop`` keeps
    a frozen compatibility façade, while the canonical runner reaches the
    explicitly internal kernel to avoid recursion back through itself.
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
        from src.agent_loop import _legacy_stream_agent_kernel

        return _legacy_stream_agent_kernel(**self.arguments(request))


@dataclass(frozen=True)
class PreparedAuthority:
    context: AgentExecutionContext
    reason: str

    def event_payload(self) -> dict[str, Any]:
        root = self.context.execution_root
        grant = self.context.authority_grant
        return {
            "type": "execution_authority",
            "mode": self.context.execution_mode.value,
            "reason": self.reason,
            "workspace": root.source.value == "selected_workspace",
            "ephemeral": True,
            "execution_root": root.path,
            "root_source": root.source.value,
            "workspace_revision": root.workspace_revision,
            "workspace_snapshot_policy": (
                "nonexecuting_git_metadata_and_bounded_workspace_content; "
                "strong_revision_required_for_approval"
            ),
            "authority_revision": grant.revision,
            "capabilities": sorted(
                capability.value for capability in grant.capabilities
            ),
            "workspace_write_granted": grant.allows(Capability.WORKSPACE_WRITE),
            "process_workspace_write_granted": grant.allows(
                Capability.PROCESS_WORKSPACE_WRITE
            ),
            "run_id": self.context.run_id,
        }


@dataclass(frozen=True)
class PreparedAgentRun:
    request: AgentRunRequest
    authority_reason: str = "provided"
    prepared_here: bool = False


class AgentRunner:
    """Own typed request preparation, authority, durability and execution."""

    def __init__(
        self,
        *,
        backend: AgentBackend | None = None,
        lifecycle_factory: Callable[[], DurableRunLifecycle] = DurableRunLifecycle,
        durable_stream: Callable[..., Any] | None = None,
        prepare_execution_context: Callable[..., Any] = prepare_execution_context_async,
    ) -> None:
        self.backend: AgentBackend = backend or AgentLoopCompatibilityBackend()
        self._lifecycle_factory = lifecycle_factory
        # Compatibility injection seam for focused tests and transitional
        # integrators. Canonical/default execution does not use the historical
        # Runtime V3 wrapper function.
        self._durable_stream = durable_stream
        self._prepare_execution_context = prepare_execution_context

    @staticmethod
    def _budgets(max_rounds: int, max_tool_calls: int) -> RunBudgets:
        effective = load_runtime_v3_limits().normalize_request(
            max_rounds=max_rounds,
            max_tool_calls=max_tool_calls,
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

    @staticmethod
    def _canonical_disabled(
        owner: str | None,
        disabled_tools,
        *,
        plan_mode: bool,
    ) -> frozenset[str]:
        disabled = set(disabled_tools or ())
        disabled.update(blocked_tools_for_owner(owner))
        if plan_mode:
            disabled.update(plan_mode_disabled_tools())
        return TOOL_REGISTRY.canonicalize_names(disabled, None)

    async def prepare_authority(
        self,
        request: AgentAuthorityRequest,
    ) -> PreparedAuthority:
        requested_mode = normalize_execution_mode(request.requested_mode)
        context, reason = await self._prepare_execution_context(
            owner_id=request.owner_id,
            session_id=str(request.session_id),
            requested_mode=requested_mode,
            selected_workspace=request.selected_workspace,
            budgets=self._budgets(request.max_rounds, request.max_tool_calls),
            tool_catalog_revision=TOOL_REGISTRY.revision,
            plan_mode=request.plan_mode,
            host_authorization_token=request.host_authorization_token,
            sandbox_default=get_setting("agent_sandbox_default_root", None),
            host_default=get_setting("agent_host_default_root", None),
            conversation_id=request.conversation_id,
            turn_lease=request.turn_lease,
            workspace_write=request.workspace_write,
            process_workspace_write=request.process_workspace_write,
            disabled_tools=self._canonical_disabled(
                request.owner_id,
                request.disabled_tools,
                plan_mode=request.plan_mode,
            ),
            defer_ownership=request.defer_ownership,
        )
        return PreparedAuthority(context=context, reason=reason)

    @staticmethod
    def _compatibility_disabled_tools(
        request: AgentRunRequest,
    ) -> frozenset[str]:
        disabled = set(request.policy.disabled_tools or ())
        policy = request.policy.policy
        if policy is not None:
            all_disabled = getattr(policy, "all_disabled_names", None)
            if callable(all_disabled):
                disabled.update(all_disabled())
        return AgentRunner._canonical_disabled(
            request.owner,
            disabled,
            plan_mode=request.plan_mode,
        )

    async def prepare(self, request: AgentRunRequest) -> PreparedAgentRun:
        if request.execution_context is not None:
            return PreparedAgentRun(request=request)
        requested = (
            request.policy.execution_mode
            if request.policy.execution_mode is not None
            else request.policy.shell_enabled
        )
        prepared = await self.prepare_authority(
            AgentAuthorityRequest(
                owner_id=request.owner,
                session_id=str(request.session_id or "compatibility-run"),
                requested_mode=requested,
                selected_workspace=request.contexts.workspace,
                max_rounds=request.limits.max_rounds,
                max_tool_calls=request.limits.max_tool_calls,
                plan_mode=request.plan_mode,
                disabled_tools=self._compatibility_disabled_tools(request),
                defer_ownership=False,
            )
        )
        return PreparedAgentRun(
            request=replace(request, execution_context=prepared.context),
            authority_reason=prepared.reason,
            prepared_here=True,
        )

    async def stream(self, request: AgentRunRequest) -> AsyncGenerator[str, None]:
        prepared = await self.prepare(request)
        typed_request = prepared.request
        backend_factory = lambda: self.backend.stream(typed_request)

        if self._durable_stream is not None:
            # Transitional injection seam. The production/default path below
            # owns a concrete durability service instance per run.
            durable = self._durable_stream(typed_request, backend_factory)
        else:
            durable = self._lifecycle_factory().stream(
                typed_request,
                backend_factory,
            )

        async for event in durable:
            yield event


DEFAULT_AGENT_RUNNER = AgentRunner()

__all__ = [
    "AgentBackend",
    "AgentLoopCompatibilityBackend",
    "AgentRunner",
    "DEFAULT_AGENT_RUNNER",
    "PreparedAgentRun",
    "PreparedAuthority",
]
