import asyncio
import os

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
    assert result["error_type"] == "approval_required"
    assert result["execution_mode"] == "disabled"


@pytest.mark.asyncio
async def test_host_mode_inherits_normal_environment_and_reports_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_HOST_MODE_PROBE", "host-visible")
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    _, result = await execute_tool_block(
        ToolBlock("bash", 'printf "%s" "$ODYSSEUS_HOST_MODE_PROBE"'),
        owner="admin",
        workspace=str(tmp_path),
        allowed_tools={"bash"},
        execution_mode="host",
    )
    assert result["exit_code"] == 0
    assert result["output"] == "host-visible"
    assert result["execution_mode"] == "host"


@pytest.mark.asyncio
async def test_host_python_uses_selected_workspace_and_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    _, result = await execute_tool_block(
        ToolBlock("python", "import os; print(os.getcwd())"),
        owner="admin",
        workspace=str(tmp_path),
        allowed_tools={"python"},
        execution_mode="host",
    )
    assert result["exit_code"] == 0
    assert result["output"] == os.path.realpath(tmp_path)
    assert result["execution_mode"] == "host"


@pytest.mark.asyncio
async def test_host_mode_is_independent_from_workspace_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.chdir(tmp_path)
    _, result = await execute_tool_block(
        ToolBlock("bash", "pwd"),
        owner="admin",
        allowed_tools={"bash"},
        execution_mode="host",
    )
    assert result["exit_code"] == 0
    assert result["output"] == str(tmp_path)
    assert result["execution_mode"] == "host"


@pytest.mark.asyncio
async def test_safe_mode_uses_server_working_tree_without_granting_file_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.chdir(tmp_path)
    _, result = await execute_tool_block(
        ToolBlock("bash", "pwd"),
        owner="admin",
        allowed_tools={"bash"},
        execution_mode="sandboxed",
    )
    assert result["exit_code"] == 0
    assert result["output"] == str(tmp_path)
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
    dispatched_modes = []

    async def provider(*args, **kwargs):
        nonlocal rounds
        rounds += 1
        if rounds == 1:
            yield 'data: {"delta": "```bash\\nprintf first\\n```"}\n\n'
        else:
            yield 'data: {"delta": "Finished."}\n\n'
            yield 'data: {"type": "finish", "reason": "stop"}\n\n'
        yield "data: [DONE]\n\n"

    async def execute(block, **kwargs):
        dispatched_modes.append(kwargs.get("execution_mode"))
        return "bash: ok", {"output": "first", "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)
    events = [
        event
        async for event in agent_loop.stream_agent_loop(
            "http://provider.invalid",
            "plain-model",
            [{"role": "user", "content": "Run and finish."}],
            relevant_tools={"bash"},
            owner="admin",
            workspace=str(tmp_path),
            shell_enabled="host",
            max_rounds=3,
            _is_teacher_run=True,
        )
    ]

    assert rounds == 2
    assert dispatched_modes == ["host"]
    diagnostic = next(
        __import__("json").loads(event[6:])
        for event in events
        if '"type": "effective_tools"' in event
    )
    assert diagnostic["execution_mode"] == "host"
