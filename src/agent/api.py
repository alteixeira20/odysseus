"""Stable entry points for the Agent runtime.

All typed Agent execution enters through :class:`src.agent.runner.AgentRunner`.
This module owns only API compatibility: construction of ``AgentRunRequest``
from the historical function signature and streaming the runner's wire output.
"""

from typing import AsyncGenerator, Dict, List, Optional, Set

from .contracts import AgentRunRequest
from .runner import DEFAULT_AGENT_RUNNER


async def stream(request: AgentRunRequest) -> AsyncGenerator[str, None]:
    async for event in DEFAULT_AGENT_RUNNER.stream(request):
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
    execution_context=None,
) -> AsyncGenerator[str, None]:
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
        execution_context=execution_context,
    )
    async for event in stream(request):
        yield event
