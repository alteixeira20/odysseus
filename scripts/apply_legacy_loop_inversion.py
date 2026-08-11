#!/usr/bin/env python3
"""Temporary asserted source materializer for the canonical loop inversion."""

from pathlib import Path

path = Path("src/agent_loop.py")
text = path.read_text(encoding="utf-8")

anchor = "\nasync def stream_agent_loop(\n"
assert text.count(anchor) == 1, "expected exactly one legacy stream definition"
assert "async def _legacy_stream_agent_kernel(" not in text
text = text.replace(
    anchor,
    "\nasync def _legacy_stream_agent_kernel(\n",
    1,
)

facade = r'''

# ---------------------------------------------------------------------------
# Frozen public compatibility façade
# ---------------------------------------------------------------------------
# The implementation above remains temporarily in this module because it owns
# characterized compatibility behavior and many helper globals. It is not a
# public orchestrator anymore. All public calls re-enter the canonical typed
# Agent API/AgentRunner; AgentLoopCompatibilityBackend reaches the internal
# kernel directly to avoid recursion.
async def stream_agent_loop(
    endpoint_url: str,
    model: str,
    messages: List[Dict],
    headers: Optional[Dict] = None,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    prompt_type: Optional[str] = None,
    max_rounds: int = MAX_AGENT_ROUNDS,
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
    tool_policy: Optional[ToolPolicy] = None,
    workspace: Optional[str] = None,
    forced_tools: Optional[Set[str]] = None,
    uploaded_files: Optional[List[Dict]] = None,
    workload: str = "foreground",
    _is_teacher_run: bool = False,
    shell_enabled: Optional[bool | str] = None,
    execution_context: Optional[AgentExecutionContext] = None,
) -> AsyncGenerator[str, None]:
    """Compatibility entry point routed through the canonical ``AgentRunner``."""

    from src.agent.api import stream_agent_loop as canonical_stream_agent_loop

    async for event in canonical_stream_agent_loop(
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
    ):
        yield event
'''

assert "Frozen public compatibility façade" not in text
text = text.rstrip() + facade + "\n"
path.write_text(text, encoding="utf-8")
