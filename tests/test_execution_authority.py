import asyncio
import os
from dataclasses import replace

import pytest

from src import agent_loop
from src.agent_tools import ToolBlock
from src.execution_policy import (
    ExecutionMode,
    normalize_execution_mode,
    resolve_execution_mode,
)
from src.tool_execution import execute_tool_block
from src import tool_execution
from src.agent.runtime_v2.authority import (
    HOST_AUTHORIZATIONS,
    prepare_execution_context,
)
from src.agent.runtime_v2.contracts import RunBudgets, ToolResult, ToolResultStatus
from src.agent.tools.bootstrap import TOOL_REGISTRY


def _execution_context(tmp_path, *, mode="sandboxed", owner="admin", session="session"):
    token = None
    if mode == "host":
        token = HOST_AUTHORIZATIONS.issue(
            owner_id=owner,
            session_id=session,
        ).token
    context, reason = prepare_execution_context(
        owner_id=owner,
        session_id=session,
        requested_mode=mode,
        selected_workspace=str(tmp_path),
        budgets=RunBudgets(),
        tool_catalog_revision=TOOL_REGISTRY.revision,
        host_authorization_token=token,
    )
    assert reason == "requested"
    return context


def test_request_mode_requires_legacy_boolean_and_explicit_host_value():
    assert resolve_execution_mode(None, allow_bash=None) is ExecutionMode.DISABLED
    assert resolve_execution_mode("host", allow_bash=False) is ExecutionMode.DISABLED
    assert resolve_execution_mode(None, allow_bash=True) is ExecutionMode.SANDBOXED
    assert resolve_execution_mode("sandboxed", allow_bash="true") is ExecutionMode.SANDBOXED
    assert resolve_execution_mode("host", allow_bash="true") is ExecutionMode.HOST
    assert resolve_execution_mode("unrecognized", allow_bash="true") is ExecutionMode.DISABLED


def test_legacy_internal_boolean_can_only_grant_sandbox():
    assert normalize_execution_mode(True, shell_enabled=True) is ExecutionMode.SANDBOXED
    assert normalize_execution_mode("host", shell_enabled=True) is ExecutionMode.HOST
    assert normalize_execution_mode(None) is ExecutionMode.DISABLED


@pytest.mark.asyncio
async def test_dispatcher_rejects_process_tool_without_typed_authority(tmp_path):
    _, result = await execute_tool_block(
        ToolBlock("bash", "printf denied"),
        owner="admin",
        workspace=str(tmp_path),
        allowed_tools={"bash"},
    )
    assert result["error_type"] == "tool_not_exposed"
    assert result["completion_state"] == "denied"


@pytest.mark.asyncio
async def test_host_mode_inherits_normal_environment_and_reports_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_HOST_MODE_PROBE", "host-visible")
    context = _execution_context(tmp_path, mode="host")
    _, result = await execute_tool_block(
        ToolBlock("bash", 'printf "%s" "$ODYSSEUS_HOST_MODE_PROBE"'),
        owner="admin",
        workspace=str(tmp_path),
        allowed_tools={"bash"},
        execution_context=context,
    )
    assert result["exit_code"] == 0
    assert result["output"] == "host-visible"
    assert result["execution_mode"] == "host"


@pytest.mark.asyncio
async def test_host_python_uses_selected_workspace_and_mode(tmp_path, monkeypatch):
    context = _execution_context(tmp_path, mode="host")
    authority = replace(
        context.authority_grant,
        approved_effects=frozenset({"process.execute.host:python"}),
    )
    context = replace(context, authority_grant=authority)
    _, result = await execute_tool_block(
        ToolBlock("python", "import os; print(os.getcwd())"),
        owner="admin",
        workspace=str(tmp_path),
        allowed_tools={"python"},
        execution_context=context,
    )
    assert result["exit_code"] == 0
    assert result["output"] == os.path.realpath(tmp_path)
    assert result["execution_mode"] == "host"


@pytest.mark.asyncio
async def test_host_mode_without_an_explicit_root_never_uses_process_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    authorization = HOST_AUTHORIZATIONS.issue(
        owner_id="admin", session_id="host-no-root"
    )
    context, reason = prepare_execution_context(
        owner_id="admin",
        session_id="host-no-root",
        requested_mode="host",
        selected_workspace=None,
        budgets=RunBudgets(),
        tool_catalog_revision=TOOL_REGISTRY.revision,
        host_authorization_token=authorization.token,
    )
    assert context.execution_mode is ExecutionMode.DISABLED
    assert context.execution_root.path != os.path.realpath(tmp_path)
    assert context.execution_root.source.value == "ephemeral_workspace"
    assert reason == "host_execution_requires_selected_or_configured_root"


@pytest.mark.asyncio
async def test_safe_mode_without_workspace_uses_ephemeral_root_not_server_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _, result = await execute_tool_block(
        ToolBlock("bash", "pwd"),
        owner="admin",
        allowed_tools={"bash"},
        execution_mode="sandboxed",
    )
    assert result["exit_code"] == 0
    assert result["output"] != str(tmp_path)
    assert result["output"].startswith("/tmp/odysseus-agent-workspace-")
    assert result["execution_mode"] == "sandboxed"


@pytest.mark.asyncio
async def test_host_authority_survives_tool_continuation_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(
        agent_loop,
        "_build_system_prompt",
        lambda messages, *args, **kwargs: (
            [{"role": "system", "content": "SYSTEM"}] + list(messages),
            [],
        ),
    )
    rounds = 0
    dispatched_contexts = []

    async def provider(*args, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            yield 'data: {"delta": "```bash\\nprintf first\\n```"}\n\n'
        else:
            yield 'data: {"delta": "Finished."}\n\n'
            yield 'data: {"type": "finish", "reason": "stop"}\n\n'
        yield "data: [DONE]\n\n"

    async def execute(call, execution_context, **kwargs):
        dispatched_contexts.append(execution_context)
        return ToolResult(
            call_id=call.call_id,
            canonical_name=call.canonical_name,
            status=ToolResultStatus.SUCCESS,
            data={"text": "first", "exit_code": 0},
            backend="test",
        )

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(
        "src.agent.execution.batch_runner.execute_normalized_tool_call",
        execute,
    )
    context = _execution_context(
        tmp_path,
        mode="host",
        session="continuation-session",
    )
    events = [
        event
        async for event in agent_loop.stream_agent_loop(
            "http://provider.invalid",
            "plain-model",
            [{"role": "user", "content": "Run and finish."}],
            relevant_tools={"bash"},
            owner="admin",
            workspace=str(tmp_path),
            execution_context=context,
            max_rounds=3,
            _is_teacher_run=True,
        )
    ]

    assert rounds == 2
    assert len(dispatched_contexts) == 1
    assert dispatched_contexts[0].run_id == context.run_id
    assert dispatched_contexts[0].authority_grant == context.authority_grant
    assert dispatched_contexts[0].execution_root == context.execution_root
    assert dispatched_contexts[0].candidate_id != context.candidate_id
    diagnostic = next(
        __import__("json").loads(event[6:])
        for event in events
        if '"type": "effective_tools"' in event
    )
    assert diagnostic["execution_mode"] == "host"
