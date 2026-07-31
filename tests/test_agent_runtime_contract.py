import asyncio
import json
import shutil
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src import agent_loop, agent_runs, bg_jobs
from src.agent_tools import ToolBlock
from src.effective_tools import (
    SHELL_FOUNDATIONAL_TOOLS,
    WORKSPACE_FOUNDATIONAL_TOOLS,
    calculate_effective_tools,
)
from src.llm_core import _normalize_reasoning_text


ALL_CODING = set(WORKSPACE_FOUNDATIONAL_TOOLS | SHELL_FOUNDATIONAL_TOOLS) | {
    "ask_user",
    "update_plan",
}


def _effective(**overrides):
    kwargs = {
        "registered_tools": ALL_CODING | {"mcp__browser__navigate"},
        "provider_usable_tools": ALL_CODING | {"mcp__browser__navigate"},
        "relevant_tools": {"ask_user"},
        "forced_tools": set(),
        "disabled_tools": set(),
        "security_blocked_tools": set(),
        "authenticated_role": "owner_admin",
        "workspace_enabled": False,
        "shell_enabled": False,
        "fallback_tools": {"ask_user", "update_plan"},
    }
    kwargs.update(overrides)
    return calculate_effective_tools(**kwargs)


def test_owner_shell_foundation_survives_relevance_and_has_prompt_schema_parity():
    effective = _effective(shell_enabled=True)
    assert SHELL_FOUNDATIONAL_TOOLS <= effective.names

    prompt = agent_loop._assemble_prompt(set(effective.names), compact=True)
    schema_names = {
        schema["function"]["name"]
        for schema in agent_loop.FUNCTION_TOOL_SCHEMAS
        if schema["function"]["name"] in effective.names
    }
    for name in SHELL_FOUNDATIONAL_TOOLS:
        assert f"`{name}`" in prompt
        assert name in schema_names


def test_explicit_shell_disable_wins_everywhere():
    effective = _effective(
        shell_enabled=True,
        disabled_tools={"bash"},
    )
    prompt = agent_loop._assemble_prompt(set(effective.names), compact=True)
    schema_names = {
        schema["function"]["name"]
        for schema in agent_loop.FUNCTION_TOOL_SCHEMAS
        if schema["function"]["name"] in effective.names
    }
    assert "bash" not in effective.names
    assert "`bash`" not in prompt
    assert "bash" not in schema_names
    assert effective.excluded_foundational["bash"] == "explicitly disabled for this run"


def test_public_security_policy_wins_over_shell_enablement():
    effective = _effective(
        shell_enabled=True,
        security_blocked_tools=set(SHELL_FOUNDATIONAL_TOOLS),
        authenticated_role="public",
    )
    assert effective.names.isdisjoint(SHELL_FOUNDATIONAL_TOOLS)
    assert "security policy for role public" in effective.excluded_foundational["bash"]


def test_workspace_foundation_is_complete_and_relevance_cannot_omit_it():
    effective = _effective(workspace_enabled=True, relevant_tools={"ask_user"})
    assert WORKSPACE_FOUNDATIONAL_TOOLS <= effective.names


def test_browser_tools_neither_replace_nor_suppress_bash():
    effective = _effective(
        shell_enabled=True,
        relevant_tools={"mcp__browser__navigate"},
        forced_tools={"mcp__browser__navigate"},
    )
    assert "mcp__browser__navigate" in effective.names
    assert "bash" in effective.names


@pytest.mark.asyncio
async def test_dispatch_rejects_tool_outside_authoritative_allowlist(monkeypatch):
    from src import tool_execution

    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    desc, result = await tool_execution.execute_tool_block(
        ToolBlock("bash", "pwd"),
        owner="admin",
        allowed_tools={"read_file"},
    )
    assert "UNAVAILABLE" in desc
    assert result["exit_code"] == 1
    assert result["unavailable"] is True


def test_no_effective_name_lacks_an_executable_registration_or_provider_surface():
    effective = _effective(workspace_enabled=True, shell_enabled=True)
    assert effective.names <= ALL_CODING
    schema_names = {
        schema["function"]["name"]
        for schema in agent_loop.FUNCTION_TOOL_SCHEMAS
    }
    assert effective.names <= schema_names


@pytest.mark.asyncio
async def test_two_concurrent_workspace_bindings_do_not_leak(tmp_path, monkeypatch):
    from src import tool_execution

    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)

    async def get_workspace(path):
        return await tool_execution.execute_tool_block(
            ToolBlock("get_workspace", ""),
            owner="admin",
            workspace=str(path),
            allowed_tools={"get_workspace"},
        )

    (_, result_one), (_, result_two) = await asyncio.gather(
        get_workspace(one),
        get_workspace(two),
    )
    assert result_one["output"].splitlines()[0] == str(one.resolve())
    assert result_two["output"].splitlines()[0] == str(two.resolve())


@pytest.mark.asyncio
async def test_concurrent_agent_runs_keep_provider_tool_scopes_isolated(
    tmp_path,
    monkeypatch,
):
    _patch_agent_environment(monkeypatch)
    captured = {}
    both_started = asyncio.Event()

    async def provider(candidates, messages, **kwargs):
        session_id = kwargs["session_id"]
        captured[session_id] = {
            schema["function"]["name"] for schema in (kwargs.get("tools") or [])
        }
        if len(captured) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        yield 'data: {"delta": "done"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)

    async def consume(session_id, workspace, shell_enabled):
        return [
            event
            async for event in agent_loop.stream_agent_loop(
                "https://api.openai.com/v1",
                "gpt-4o",
                [{"role": "user", "content": "Respond with done."}],
                relevant_tools={"ask_user"},
                owner="admin",
                session_id=session_id,
                workspace=str(workspace),
                shell_enabled=shell_enabled,
                max_rounds=1,
            )
        ]

    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    events_a, events_b = await asyncio.gather(
        consume("scope-a", one, True),
        consume("scope-b", two, False),
    )

    assert "bash" in captured["scope-a"]
    assert "bash" not in captured["scope-b"]
    assert events_a[-1] == "data: [DONE]\n\n"
    assert events_b[-1] == "data: [DONE]\n\n"


def test_tmux_workspace_key_handles_spaces_and_apostrophes():
    from src.agent_tools.subprocess_tools import _tmux_session_name

    first = _tmux_session_name("chat", "/tmp/a workspace/it's here")
    second = _tmux_session_name("chat", "/tmp/another workspace")
    assert first != second
    assert " " not in first
    assert "'" not in first


@pytest.mark.asyncio
async def test_real_tmux_shell_preserves_same_workspace_cd_and_rebinds_on_change(
    tmp_path,
    monkeypatch,
):
    if not shutil.which("tmux"):
        pytest.skip("tmux is not installed")

    from src.agent_tools.subprocess_tools import _run_exec, _tmux_session_name
    from src import tool_execution

    one = tmp_path / "first workspace's repo"
    two = tmp_path / "second workspace"
    (one / "src").mkdir(parents=True)
    two.mkdir()
    session_id = f"runtime-contract-{uuid.uuid4().hex}"
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)

    async def bash(command, workspace):
        return await tool_execution.execute_tool_block(
            ToolBlock("bash", command),
            owner="admin",
            session_id=session_id,
            workspace=str(workspace),
            allowed_tools={"bash"},
        )

    try:
        _, changed = await bash("cd src", one)
        _, same_workspace = await bash("pwd", one)
        _, changed_workspace = await bash("pwd", two)
        _, restored_workspace = await bash("pwd", one)

        assert changed["exit_code"] == 0
        assert Path(same_workspace["output"]).resolve() == (one / "src").resolve()
        assert Path(changed_workspace["output"]).resolve() == two.resolve()
        assert Path(restored_workspace["output"]).resolve() == (one / "src").resolve()
    finally:
        for workspace in (one, two):
            await _run_exec(
                "tmux",
                "kill-session",
                "-t",
                _tmux_session_name(session_id, str(workspace)),
                timeout=5,
            )


@pytest.mark.asyncio
async def test_delayed_provider_emits_rate_limited_status_then_output():
    async def delayed():
        await asyncio.sleep(0.035)
        yield 'data: {"delta": "ready"}\n\n'
        yield "data: [DONE]\n\n"

    events = [
        event
        async for event in agent_loop._stream_with_idle_status(
            delayed(),
            interval_s=0.01,
        )
    ]
    statuses = [event for event in events if '"type": "run_status"' in event]
    assert 2 <= len(statuses) <= 5
    assert any('"delta": "ready"' in event for event in events)


def test_structured_reasoning_normalization_accepts_text_only():
    value = [
        {"type": "reasoning", "text": "one"},
        {"reasoning": {"content": [{"text": " two"}], "signature": "secret"}},
        {"metadata": {"private": "must not stringify"}},
        42,
    ]
    assert _normalize_reasoning_text(value) == "one two"


def _patch_agent_environment(monkeypatch):
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)

    def prompt(messages, model, active_document, mcp_mgr, disabled_tools, **kwargs):
        names = set(kwargs.get("effective_tool_names") or ())
        return (
            [{"role": "system", "content": agent_loop._assemble_prompt(names, compact=True)}]
            + list(messages),
            [],
        )

    monkeypatch.setattr(agent_loop, "_build_system_prompt", prompt)
    try:
        from services.memory.skills import SkillsManager

        monkeypatch.setattr(SkillsManager, "load", lambda self, owner=None: [])
    except Exception:
        pass


async def _capture_agent_contract(
    monkeypatch,
    *,
    workspace,
    shell_enabled,
    security_blocked=frozenset(),
    message="fix the repository",
):
    _patch_agent_environment(monkeypatch)
    monkeypatch.setattr(
        agent_loop,
        "blocked_tools_for_owner",
        lambda owner: set(security_blocked),
    )
    captured = {}

    async def provider(candidates, messages, **kwargs):
        captured["messages"] = messages
        captured["schemas"] = kwargs.get("tools") or []
        yield 'data: {"delta": "done"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    events = [
        event
        async for event in agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": message}],
            relevant_tools={"ask_user"},
            owner="admin" if not security_blocked else "public",
            workspace=str(workspace) if workspace else None,
            shell_enabled=shell_enabled,
            max_rounds=1,
        )
    ]
    diagnostic = next(
        json.loads(event[6:])
        for event in events
        if event.startswith("data: {") and '"type": "effective_tools"' in event
    )
    return captured, diagnostic


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workspace_enabled", "shell_enabled"),
    ((False, False), (False, True), (True, False), (True, True)),
)
async def test_runtime_workspace_shell_provider_schema_matrix(
    tmp_path,
    monkeypatch,
    workspace_enabled,
    shell_enabled,
):
    captured, diagnostic = await _capture_agent_contract(
        monkeypatch,
        workspace=tmp_path if workspace_enabled else None,
        shell_enabled=shell_enabled,
        message="Respond with done.",
    )
    schema_names = {
        schema["function"]["name"] for schema in captured["schemas"]
    }

    assert ("bash" in schema_names) is shell_enabled
    assert ("bash" in diagnostic["names"]) is shell_enabled
    assert (WORKSPACE_FOUNDATIONAL_TOOLS <= schema_names) is workspace_enabled


@pytest.mark.asyncio
async def test_runtime_uses_one_effective_set_for_prompt_schemas_and_diagnostic(
    tmp_path,
    monkeypatch,
):
    captured, diagnostic = await _capture_agent_contract(
        monkeypatch,
        workspace=tmp_path,
        shell_enabled=True,
    )
    schema_names = {
        schema["function"]["name"] for schema in captured["schemas"]
    }
    prompt = captured["messages"][0]["content"]
    assert ALL_CODING <= schema_names
    assert ALL_CODING <= set(diagnostic["names"])
    assert all(f"`{name}`" in prompt for name in ALL_CODING)
    assert diagnostic["ephemeral"] is True


@pytest.mark.asyncio
async def test_runtime_explicit_shell_disable_removes_shell_only(
    tmp_path,
    monkeypatch,
):
    captured, diagnostic = await _capture_agent_contract(
        monkeypatch,
        workspace=tmp_path,
        shell_enabled=False,
    )
    schema_names = {
        schema["function"]["name"] for schema in captured["schemas"]
    }
    assert schema_names.isdisjoint(SHELL_FOUNDATIONAL_TOOLS)
    assert set(diagnostic["names"]).isdisjoint(SHELL_FOUNDATIONAL_TOOLS)
    assert WORKSPACE_FOUNDATIONAL_TOOLS <= schema_names
    assert "`bash`" not in captured["messages"][0]["content"]


@pytest.mark.asyncio
async def test_runtime_public_security_gate_wins_over_requested_coding_tools(
    tmp_path,
    monkeypatch,
):
    blocked = set(SHELL_FOUNDATIONAL_TOOLS | WORKSPACE_FOUNDATIONAL_TOOLS)
    captured, diagnostic = await _capture_agent_contract(
        monkeypatch,
        workspace=tmp_path,
        shell_enabled=True,
        security_blocked=blocked,
    )
    schema_names = {
        schema["function"]["name"] for schema in captured["schemas"]
    }
    assert schema_names.isdisjoint(blocked)
    assert set(diagnostic["names"]).isdisjoint(blocked)
    assert "security policy for role public" in diagnostic["excluded_foundational"]["bash"]


@pytest.mark.asyncio
async def test_runtime_normalizes_non_numeric_settings_once_per_run(
    monkeypatch,
    tmp_path,
):
    _patch_agent_environment(monkeypatch)
    reads = {}

    def malformed_settings(key, default=None):
        reads[key] = reads.get(key, 0) + 1
        if key in {
            "agent_stream_timeout_seconds",
            "agent_input_token_budget",
            "agent_input_token_hard_max",
            "agent_max_tool_calls",
            "skill_max_injected",
            "skill_autosave_min_confidence",
        }:
            return "not-a-number"
        return default

    async def provider(*args, **kwargs):
        yield 'data: {"delta": "done"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "get_setting", malformed_settings)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)

    events = [
        event
        async for event in agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "inspect the repository architecture"}],
            relevant_tools={"ask_user"},
            workspace=str(tmp_path),
            shell_enabled=False,
            max_rounds=1,
        )
    ]

    assert events[-1] == "data: [DONE]\n\n"
    assert reads["agent_stream_timeout_seconds"] == 1
    assert reads["agent_input_token_budget"] == 1
    assert reads["agent_input_token_hard_max"] == 1


@pytest.mark.asyncio
async def test_partial_visible_output_then_failure_is_not_replayed(monkeypatch):
    _patch_agent_environment(monkeypatch)
    calls = 0

    async def provider(*args, **kwargs):
        nonlocal calls
        calls += 1
        yield 'data: {"delta": "partial"}\n\n'
        yield 'event: error\ndata: {"status": 502, "text": "Bad Gateway"}\n\n'

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    events = [
        event
        async for event in agent_loop.stream_agent_loop(
            "http://provider.invalid",
            "plain-model",
            [{"role": "user", "content": "do the task"}],
            relevant_tools={"ask_user"},
            shell_enabled=False,
        )
    ]
    assert calls == 1
    assert sum('"delta": "partial"' in event for event in events) == 1
    assert sum("event: error" in event for event in events) == 1
    assert events[-1] == "data: [DONE]\n\n"


@pytest.mark.asyncio
async def test_partial_tool_arguments_then_failure_never_dispatches(monkeypatch):
    _patch_agent_environment(monkeypatch)
    execute = AsyncMock()

    async def provider(*args, **kwargs):
        yield 'data: {"type": "tool_call_delta", "name": "read_file", "arg_delta": "{\\"path\\":"}\n\n'
        yield 'event: error\ndata: {"status": 502, "text": "Bad Gateway"}\n\n'

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)
    events = [
        event
        async for event in agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "read it"}],
            relevant_tools={"read_file"},
            shell_enabled=False,
        )
    ]
    execute.assert_not_awaited()
    assert sum("event: error" in event for event in events) == 1


@pytest.mark.asyncio
async def test_failed_fenced_tool_can_be_corrected_next_round(monkeypatch, tmp_path):
    _patch_agent_environment(monkeypatch)
    provider_round = 0
    tool_calls = []

    async def provider(*args, **kwargs):
        nonlocal provider_round
        provider_round += 1
        if provider_round == 1:
            yield 'data: {"delta": "```read_file\\nmissing.py\\n```"}\n\n'
        elif provider_round == 2:
            yield 'data: {"delta": "```read_file\\nexisting.py\\n```"}\n\n'
        else:
            yield 'data: {"delta": "Completed after correcting the path."}\n\n'
        yield "data: [DONE]\n\n"

    async def execute(block, **kwargs):
        tool_calls.append(block.content.strip())
        if "missing.py" in block.content:
            return "read_file: missing.py", {"error": "not found", "exit_code": 1}
        return "read_file: existing.py", {"content": "ok", "size": 2, "exit_code": 0}

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(agent_loop, "execute_tool_block", execute)
    events = [
        event
        async for event in agent_loop.stream_agent_loop(
            "http://provider.invalid",
            "plain-model",
            [{"role": "user", "content": "read the file and finish"}],
            relevant_tools={"read_file"},
            workspace=str(tmp_path),
            shell_enabled=False,
            max_rounds=4,
        )
    ]
    assert provider_round == 3
    assert tool_calls == ["missing.py", "existing.py"]
    assert any("Completed after correcting the path." in event for event in events)


@pytest.mark.asyncio
async def test_retry_after_sleep_is_cancellable(monkeypatch):
    _patch_agent_environment(monkeypatch)
    entered_backoff = asyncio.Event()
    real_sleep = asyncio.sleep

    async def provider(*args, **kwargs):
        yield 'event: error\ndata: {"status": 429, "text": "rate limited", "retry_after": 30}\n\n'

    async def sleep(delay):
        if delay == 30:
            entered_backoff.set()
        await real_sleep(delay)

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(agent_loop.asyncio, "sleep", sleep)

    async def consume():
        async for _ in agent_loop.stream_agent_loop(
            "http://provider.invalid",
            "plain-model",
            [{"role": "user", "content": "continue"}],
            relevant_tools={"ask_user"},
            shell_enabled=False,
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered_backoff.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_last_client_disconnect_cancels_provider_run(monkeypatch):
    session_id = "disconnect-contract"
    cancelled = asyncio.Event()
    monkeypatch.setattr(bg_jobs, "kill_for_session_since", lambda *args: 0)

    async def provider():
        try:
            yield 'data: {"type": "run_status", "phase": "contacting_provider"}\n\n'
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    run = agent_runs.start(session_id, provider())
    subscriber = agent_runs.subscribe(session_id)
    await subscriber.__anext__()
    await subscriber.aclose()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)
    assert run.status == "stopped"
    assert any('"state": "cancelled"' in event for event in run.buffer)
    agent_runs._RUNS.pop(session_id, None)


def test_cancelled_run_kills_only_jobs_started_by_that_run(monkeypatch):
    jobs = {
        "old": {"session_id": "s", "status": "running", "started_at": 1, "pid": 1},
        "new": {"session_id": "s", "status": "running", "started_at": 10, "pid": 2},
        "other": {"session_id": "other", "status": "running", "started_at": 10, "pid": 3},
    }
    killed = []
    monkeypatch.setattr(bg_jobs, "_load", lambda: jobs)
    monkeypatch.setattr(bg_jobs, "_save", lambda value: None)
    monkeypatch.setattr(bg_jobs, "_kill", lambda pid: killed.append(pid))
    assert bg_jobs.kill_for_session_since("s", 5) == 1
    assert killed == [2]
    assert jobs["old"]["status"] == "running"
    assert jobs["new"]["cancelled_with_run"] is True


def test_frontend_separates_status_reasoning_answer_and_terminal_state():
    source = Path("static/js/chat.js").read_text(encoding="utf-8")
    assert "agent-run-status" in source
    assert "json.type === 'run_status'" in source
    assert "json.type === 'run_state'" in source
    assert "if (json.thinking)" in source
    assert "_clearRunStatus();" in source
    assert "typewriterInto(" not in source


def test_frontend_does_not_override_explicit_shell_toggle():
    source = Path("static/js/chat.js").read_text(encoding="utf-8")
    request_source = Path("static/js/agentToolRequest.js").read_text(encoding="utf-8")
    assert "appendAgentToolRequestFields(fd" in source
    assert "allow_bash: shellEnabled ? 'true' : 'false'" in request_source
    assert "fd.set('allow_bash', 'true')" not in source
