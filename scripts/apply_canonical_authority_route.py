#!/usr/bin/env python3
from pathlib import Path

path = Path("routes/chat_routes.py")
text = path.read_text(encoding="utf-8")

old_import = "from src.agent.api import stream_agent_loop\n"
new_import = (
    "from src.agent.api import prepare_authority, stream_agent_loop\n"
    "from src.agent.contracts import AgentAuthorityRequest\n"
)
assert text.count(old_import) == 1, "stable agent API import anchor changed"
text = text.replace(old_import, new_import, 1)

start_marker = "        _execution_context = None\n        _tool_budget = 0\n        _max_rounds = 1\n        if _effective_mode == \"agent\":\n"
end_marker = "\n        async def stream_with_save() -> AsyncGenerator[str, None]:\n"
assert text.count(start_marker) == 1, "authority block start changed"
start = text.index(start_marker)
end = text.index(end_marker, start)
new_block = '''        _execution_context = None
        _authority_event = None
        _tool_budget = 0
        _max_rounds = 1
        if _effective_mode == "agent":
            from src.agent_tools import MAX_AGENT_ROUNDS as _DEFAULT_ROUNDS

            try:
                _tool_budget = int(get_setting("agent_max_tool_calls", 0))
            except (TypeError, ValueError):
                _tool_budget = 0
            try:
                _max_rounds = int(
                    get_setting("agent_max_rounds", _DEFAULT_ROUNDS)
                    or _DEFAULT_ROUNDS
                )
            except (TypeError, ValueError):
                _max_rounds = _DEFAULT_ROUNDS
            _max_rounds = max(1, min(_max_rounds, 200))

            _prepared_authority = await prepare_authority(
                AgentAuthorityRequest(
                    owner_id=str(_user or ""),
                    session_id=str(session),
                    requested_mode=execution_mode,
                    selected_workspace=workspace or None,
                    max_rounds=_max_rounds,
                    max_tool_calls=_tool_budget,
                    plan_mode=plan_mode,
                    host_authorization_token=(
                        str(host_authorization_token)
                        if host_authorization_token
                        else None
                    ),
                    conversation_id=str(session),
                    turn_lease=_prepared_turn.lease,
                    workspace_write=(
                        str(allow_workspace_write).strip().lower() == "true"
                        and bool(workspace)
                        and not plan_mode
                    ),
                    process_workspace_write=(
                        str(allow_process_workspace_write).strip().lower() == "true"
                        and bool(workspace)
                        and shell_enabled
                        and not plan_mode
                    ),
                    disabled_tools=frozenset(disabled_tools),
                    defer_ownership=True,
                )
            )
            _execution_context = _prepared_authority.context
            _authority_event = _prepared_authority.event_payload()
            execution_mode = _execution_context.execution_mode
            shell_enabled = execution_mode.enabled
            if _prepared_authority.reason != "requested":
                shell_mode_reason = _prepared_authority.reason
'''
text = text[:start] + new_block + text[end:]

payload_start = '''                    yield "data: " + json.dumps({
                        "type": "execution_authority",
'''
payload_end = '''                    }) + "\\n\\n"

                    async for chunk in stream_agent_loop(
'''
assert text.count(payload_start) == 1, "authority SSE block start changed"
pstart = text.index(payload_start)
pend = text.index(payload_end, pstart) + len('                    }) + "\\n\\n"\n')
replacement = '''                    assert _authority_event is not None
                    yield "data: " + json.dumps(_authority_event) + "\\n\\n"
'''
text = text[:pstart] + replacement + text[pend:]

assert "from src.agent.runtime_v2.authority import prepare_execution_context_async" not in text
assert "Capability.WORKSPACE_WRITE" not in text
path.write_text(text, encoding="utf-8")
