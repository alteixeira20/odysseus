"""Stable entry points for the strangled agent runtime."""

from typing import AsyncGenerator, Dict, List, Optional, Set

from .contracts import AgentRunRequest


def _legacy_stream(**arguments):
    """Resolve the compatibility runtime lazily to avoid import cycles."""

    from src.agent_loop import stream_agent_loop as legacy_stream_agent_loop

    return legacy_stream_agent_loop(**arguments)


async def stream(request: AgentRunRequest) -> AsyncGenerator[str, None]:
    """Stream one typed agent request using the current runtime implementation."""

    arguments = {
        "endpoint_url": request.endpoint_url,
        "model": request.model,
        "messages": request.messages,
        "headers": request.model_options.headers,
        "temperature": request.model_options.temperature,
        "max_tokens": request.limits.max_tokens,
        "prompt_type": request.model_options.prompt_type,
        "max_rounds": request.limits.max_rounds,
        "max_tool_calls": request.limits.max_tool_calls,
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
    }
    async for event in _legacy_stream(**arguments):
        yield event


async def stream_agent_loop(
    endpoint_url: str,
    model: str,
    messages: List[Dict],
    headers: Optional[Dict] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    prompt_type: Optional[str] = None,
    max_rounds: int = 50,
    max_tool_calls: int = 0,
    context_length: int = 0,
    active_document=None,
    active_email: Optional[Dict[str, str]] = None,
    session_id: Optional[str] = None,
    disabled_tools: Optional[Set[str]] = None,
    owner: Optional[str] = None,
    relevant_tools: Optional[Set[str]] = None,
    fallbacks: Optional[List[tuple]] = None,
    plan_mode: bool = False,
    approved_plan: Optional[str] = None,
    tool_policy=None,
    workspace: Optional[str] = None,
    forced_tools: Optional[Set[str]] = None,
    uploaded_files: Optional[List[Dict]] = None,
    workload: str = "foreground",
    _is_teacher_run: bool = False,
    shell_enabled: Optional[bool | str] = None,
) -> AsyncGenerator[str, None]:
    """Compatibility-shaped route entry point backed by a typed request."""

    request = AgentRunRequest.from_legacy_arguments(
        endpoint_url=endpoint_url,
        model=model,
        messages=messages,
        headers=headers,
        temperature=temperature,
        max_tokens=max_tokens,
        prompt_type=prompt_type,
        max_rounds=max_rounds,
        max_tool_calls=max_tool_calls,
        context_length=context_length,
        active_document=active_document,
        active_email=active_email,
        session_id=session_id,
        disabled_tools=disabled_tools,
        owner=owner,
        relevant_tools=relevant_tools,
        fallbacks=fallbacks,
        plan_mode=plan_mode,
        approved_plan=approved_plan,
        tool_policy=tool_policy,
        workspace=workspace,
        forced_tools=forced_tools,
        uploaded_files=uploaded_files,
        workload=workload,
        _is_teacher_run=_is_teacher_run,
        shell_enabled=shell_enabled,
    )
    async for event in stream(request):
        yield event
