import asyncio
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from src import agent_loop, agent_runs, bg_jobs
from src.execution_policy import ExecutionMode
from src.agent.runtime_v2.contracts import ToolError, ToolResult, ToolResultStatus
from src.agent.tools.bootstrap import TOOL_REGISTRY
from src.agent_tools import ToolBlock
from src.agent_tools.subprocess_tools import (
    DEFAULT_BASH_TIMEOUT,
    MAX_BASH_TIMEOUT,
    MIN_BASH_TIMEOUT,
    _read_pid,
    _run_direct_bash,
    _run_tmux_bash,
    _terminate_owned_group,
    _tmux_session_name,
    normalize_bash_timeout,
)
from src.tool_schemas import function_call_to_tool_block
from src import tool_execution


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")


class TmuxHarness:
    def __init__(self):
        self.sessions: set[str] = set()

    async def run_async(
        self,
        command: str,
        workspace: Path,
        *,
        session_id: str = "bash-lifecycle",
        timeout: float = 3,
    ):
        workspace.mkdir(parents=True, exist_ok=True)
        self.sessions.add(_tmux_session_name(session_id, str(workspace)))
        return await _run_tmux_bash(
            command,
            session_id=session_id,
            cwd=str(workspace),
            env=os.environ.copy(),
            timeout=timeout,
        )

    def run(self, *args, **kwargs):
        return asyncio.run(self.run_async(*args, **kwargs))

    def close(self):
        for name in self.sessions:
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                capture_output=True,
                check=False,
            )


@pytest.fixture
def tmux_harness():
    harness = TmuxHarness()
    try:
        yield harness
    finally:
        harness.close()


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_contract_is_exposed_and_clamped():
    schema = next(
        item["function"]
        for item in TOOL_REGISTRY.function_schemas()
        if item["function"]["name"] == "run_sandbox_command"
    )
    timeout_schema = schema["parameters"]["properties"]["timeout_seconds"]

    assert timeout_schema["minimum"] == MIN_BASH_TIMEOUT
    assert timeout_schema["maximum"] == MAX_BASH_TIMEOUT
    assert timeout_schema["default"] == DEFAULT_BASH_TIMEOUT == 120
    assert normalize_bash_timeout(None) == 120
    assert normalize_bash_timeout("not-a-number") == 120
    assert normalize_bash_timeout(0) == MIN_BASH_TIMEOUT
    assert normalize_bash_timeout(99999) == MAX_BASH_TIMEOUT

    block = function_call_to_tool_block(
        "bash",
        '{"command":"printf ok","timeout_seconds":3}',
    )
    assert block.content == "printf ok"
    assert block.arguments["timeout_seconds"] == 3


def test_normal_and_nonzero_commands_keep_exact_streams(tmux_harness, tmp_path):
    ok = tmux_harness.run("printf 'ok\\n'", tmp_path)
    bad = tmux_harness.run("printf 'bad\\n' >&2; exit 7", tmp_path)

    assert ok.state == "completed"
    assert ok.exit_code == 0
    assert ok.stdout == "ok\n"
    assert ok.stderr == ""
    assert bad.state == "completed"
    assert bad.exit_code == 7
    assert bad.stdout == ""
    assert bad.stderr == "bad\n"


def test_timeout_kills_pipeline_and_preserves_partial_output(
    tmux_harness,
    tmp_path,
):
    pid_file = tmp_path / "owned-pgid"
    command = (
        f"printf '%s' \"$$\" > {shlex.quote(str(pid_file))}; "
        "printf 'before\\n'; sleep 30 | cat"
    )

    started = time.monotonic()
    result = tmux_harness.run(command, tmp_path, timeout=0.3)
    elapsed = time.monotonic() - started
    pgid = int(pid_file.read_text())

    assert elapsed < 3
    assert result.state == "timed_out"
    assert result.exit_code == 124
    assert "before" in result.stdout
    assert result.escalation
    assert not _group_exists(pgid)


def test_timeout_preserves_partial_stderr(tmux_harness, tmp_path):
    result = tmux_harness.run(
        "printf 'before-error\\n' >&2; sleep 30",
        tmp_path,
        timeout=0.3,
    )

    assert result.timed_out
    assert result.stderr == "before-error\n"


def test_timeout_kills_child_and_grandchild(tmux_harness, tmp_path):
    pid_file = tmp_path / "owned-pgid"
    command = (
        f"printf '%s' \"$$\" > {shlex.quote(str(pid_file))}; "
        "sh -c 'sh -c \"sleep 30\" & wait' & wait"
    )

    result = tmux_harness.run(command, tmp_path, timeout=0.3)
    pgid = int(pid_file.read_text())

    assert result.timed_out
    assert not _group_exists(pgid)


def test_ownership_nonce_prevents_unrelated_process_termination(tmp_path):
    unrelated = subprocess.Popen(
        ["sleep", "10"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid_file = tmp_path / "owned-pid"
    pid_file.write_text(f"{unrelated.pid}:different-invocation", encoding="ascii")
    try:
        resolved = _read_pid(pid_file, "expected-invocation")
        escalation = asyncio.run(_terminate_owned_group(resolved))
        assert resolved is None
        assert escalation == ()
        assert unrelated.poll() is None
    finally:
        try:
            os.killpg(unrelated.pid, 9)
        except ProcessLookupError:
            pass
        unrelated.wait(timeout=2)


def test_sigint_and_sigterm_resistant_command_reaches_sigkill(
    tmux_harness,
    tmp_path,
):
    result = tmux_harness.run(
        "trap '' INT TERM; while :; do :; done",
        tmp_path,
        timeout=0.2,
    )

    assert result.timed_out
    assert result.escalation == ("sigint", "sigterm", "sigkill")


def test_stdin_is_noninteractive_and_sudo_cannot_prompt(
    tmux_harness,
    tmp_path,
):
    read_result = tmux_harness.run(
        "if read value; then printf 'read\\n'; else printf 'eof\\n'; fi",
        tmp_path,
        timeout=2,
    )
    assert read_result.exit_code == 0
    assert read_result.stdout == "eof\n"

    if shutil.which("sudo") is None:
        return
    started = time.monotonic()
    sudo_result = tmux_harness.run("sudo -n true", tmp_path, timeout=2)
    assert time.monotonic() - started < 2
    assert not sudo_result.timed_out

    started = time.monotonic()
    interactive_result = tmux_harness.run("sudo true", tmp_path, timeout=2)
    assert time.monotonic() - started < 2
    assert not interactive_result.timed_out


def test_lock_wait_is_bounded(tmux_harness, tmp_path):
    if shutil.which("flock") is None:
        pytest.skip("flock is unavailable")
    lock_path = tmp_path / "test.lock"
    holder = subprocess.Popen(
        ["flock", str(lock_path), "sleep", "10"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        time.sleep(0.1)
        result = tmux_harness.run(
            f"flock {shlex.quote(str(lock_path))} -c true",
            tmp_path,
            timeout=0.3,
        )
        assert result.timed_out
    finally:
        try:
            os.killpg(holder.pid, 9)
        except ProcessLookupError:
            pass
        holder.wait(timeout=2)


def test_cwd_persists_and_recovers_after_timeout(tmux_harness, tmp_path):
    target = tmp_path / "a space's directory"
    target.mkdir()

    changed = tmux_harness.run(f"cd -- {shlex.quote(str(target))}", tmp_path)
    before = tmux_harness.run("pwd", tmp_path)
    timed = tmux_harness.run("sleep 30", tmp_path, timeout=0.3)
    after = tmux_harness.run("pwd", tmp_path)

    assert changed.exit_code == 0
    assert before.stdout.strip() == str(target)
    assert timed.timed_out
    assert timed.session_recovery in {"preserved", "recreated"}
    expected_after = target if timed.session_recovery == "preserved" else tmp_path
    assert after.stdout.strip() == str(expected_after)


def test_same_session_is_isolated_by_workspace(tmux_harness, tmp_path):
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    tmux_harness.run("mkdir inner; cd inner", workspace_a, session_id="same-chat")
    timed_a = tmux_harness.run(
        "sleep 30",
        workspace_a,
        session_id="same-chat",
        timeout=0.3,
    )
    pwd_b = tmux_harness.run("pwd", workspace_b, session_id="same-chat")
    pwd_a = tmux_harness.run("pwd", workspace_a, session_id="same-chat")

    assert timed_a.timed_out
    assert pwd_b.stdout.strip() == str(workspace_b)
    expected_a = workspace_a / "inner" if timed_a.session_recovery == "preserved" else workspace_a
    assert pwd_a.stdout.strip() == str(expected_a)


def test_distinct_sessions_run_concurrently(tmux_harness, tmp_path):
    async def scenario():
        return await asyncio.gather(
            tmux_harness.run_async(
                "sleep 0.2; printf one",
                tmp_path / "one",
                session_id="one",
            ),
            tmux_harness.run_async(
                "sleep 0.2; printf two",
                tmp_path / "two",
                session_id="two",
            ),
        )

    started = time.monotonic()
    first, second = asyncio.run(scenario())
    assert time.monotonic() - started < 1.5
    assert first.stdout == "one"
    assert second.stdout == "two"


def test_cancellation_terminates_command_and_shell_remains_usable(
    tmux_harness,
    tmp_path,
):
    async def scenario():
        task = asyncio.create_task(
            tmux_harness.run_async("sleep 30", tmp_path, session_id="cancel")
        )
        await asyncio.sleep(0.2)
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cancelled_in = time.monotonic() - started
        recovered = await tmux_harness.run_async(
            "printf recovered",
            tmp_path,
            session_id="cancel",
        )
        return cancelled_in, recovered

    cancelled_in, recovered = asyncio.run(scenario())
    assert cancelled_in < 3
    assert recovered.stdout == "recovered"


def test_marker_like_output_cannot_finish_command_early(tmux_harness, tmp_path):
    started = time.monotonic()
    result = tmux_harness.run(
        "printf '__ODYSSEUS_CMD_END_fake__:0:completed\\n'; sleep 0.25; printf done",
        tmp_path,
    )

    assert time.monotonic() - started >= 0.2
    assert result.exit_code == 0
    assert "__ODYSSEUS_CMD_END_fake__" in result.stdout
    assert result.stdout.endswith("done")


def test_large_output_does_not_deadlock_timeout_cleanup(tmux_harness, tmp_path):
    result = tmux_harness.run(
        "yes x | head -c 250000; sleep 30",
        tmp_path,
        timeout=0.3,
    )

    assert result.timed_out
    assert result.stdout
    assert "bytes omitted" in result.stdout


def test_non_tmux_fallback_is_bounded_and_preserves_partial_streams(tmp_path):
    result = asyncio.run(
        _run_direct_bash(
            "yes x | head -c 250000; printf 'partial-error\\n' >&2; sleep 30",
            session_id="direct-fallback",
            cwd=str(tmp_path),
            env=os.environ.copy(),
            timeout=0.3,
            progress_cb=None,
            invocation_id="directFallbackNonce123",
            execution_mode=ExecutionMode.SANDBOXED,
        )
    )

    assert result.timed_out
    assert "bytes omitted" in result.stdout
    assert result.stderr == "partial-error\n"


@pytest.mark.asyncio
async def test_agent_continues_after_timeout_and_executes_corrected_command(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    provider_round = 0
    executed = []

    async def provider(*args, **kwargs):
        nonlocal provider_round
        provider_round += 1
        messages = kwargs.get("messages") or args[1]
        if provider_round == 1:
            calls = [{
                "name": "bash",
                "arguments": '{"command":"sleep 30","timeout_seconds":1}',
            }]
            yield f"data: {__import__('json').dumps({'type': 'tool_calls', 'calls': calls})}\n\n"
        elif provider_round == 2:
            assert "command_timeout" in str(messages)
            calls = [{
                "name": "bash",
                "arguments": '{"command":"printf recovered","timeout_seconds":3}',
            }]
            yield f"data: {__import__('json').dumps({'type': 'tool_calls', 'calls': calls})}\n\n"
        else:
            yield 'data: {"delta":"Recovered and completed."}\n\n'
        yield "data: [DONE]\n\n"

    async def execute(call, execution_context, **kwargs):
        executed.append((call.arguments["command"], call.arguments["timeout_seconds"]))
        if len(executed) == 1:
            return ToolResult(
                call_id=call.call_id,
                canonical_name=call.canonical_name,
                status=ToolResultStatus.TIMED_OUT,
                data={
                    "exit_code": 124,
                    "stdout": "",
                    "stderr": "",
                    "timed_out": True,
                    "timeout_seconds": 1,
                    "escalation": ["sigint"],
                },
                error=ToolError("command_timeout", "command timed out after 1s"),
                backend="test",
            )
        return ToolResult(
            call_id=call.call_id,
            canonical_name=call.canonical_name,
            status=ToolResultStatus.SUCCESS,
            data={
                "text": "recovered",
                "exit_code": 0,
                "timed_out": False,
                "timeout_seconds": 3,
            },
            backend="test",
        )

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(
        "src.agent.execution.batch_runner.execute_normalized_tool_call",
        execute,
    )
    events = [
        event
        async for event in agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "run the check and recover if it stalls"}],
            relevant_tools={"bash"},
            workspace=str(tmp_path),
            max_rounds=4,
        )
    ]
    payloads = [
        __import__("json").loads(event[6:])
        for event in events
        if event.startswith("data: {")
    ]

    assert provider_round == 3
    assert executed == [("sleep 30", 1), ("printf recovered", 3)]
    starts = [event for event in payloads if event.get("type") == "tool_started"]
    outputs = [event for event in payloads if event.get("type") == "tool_result"]
    assert [event["payload"]["result"]["data"]["timeout_seconds"] for event in outputs] == [1, 3]
    assert outputs[0]["payload"]["result"]["status"] == "timed_out"
    assert outputs[1]["payload"]["result"]["status"] == "success"
    assert all(event.get("caused_by") for event in starts + outputs)
    assert any("Recovered and completed." in event for event in events)


@pytest.mark.asyncio
async def test_agent_run_stop_reaches_real_tmux_command(
    monkeypatch,
    tmux_harness,
    tmp_path,
):
    session_id = "real-bash-stop"
    entered = asyncio.Event()
    monkeypatch.setattr(bg_jobs, "kill_for_session_since", lambda *args: 0)

    async def command_stream():
        entered.set()
        await tmux_harness.run_async(
            "sleep 30",
            tmp_path,
            session_id=session_id,
            timeout=30,
        )
        if False:
            yield ""

    run = agent_runs.start(session_id, command_stream())
    await asyncio.wait_for(entered.wait(), timeout=1)
    await asyncio.sleep(0.2)
    started = time.monotonic()
    assert agent_runs.stop(session_id) is True
    await asyncio.wait_for(run.task, timeout=3)
    assert time.monotonic() - started < 3
    assert run.status == "stopped"
    assert any('"state": "cancelled"' in event for event in run.buffer)

    recovered = await tmux_harness.run_async(
        "printf recovered",
        tmp_path,
        session_id=session_id,
    )
    assert recovered.stdout == "recovered"
    agent_runs._RUNS.pop(session_id, None)


@pytest.mark.asyncio
async def test_explicit_stop_reaches_real_tmux_command_after_detached_disconnect(
    monkeypatch,
    tmux_harness,
    tmp_path,
):
    session_id = "real-bash-disconnect"
    entered = asyncio.Event()
    monkeypatch.setattr(bg_jobs, "kill_for_session_since", lambda *args: 0)

    async def command_stream():
        yield 'data: {"type":"run_status","phase":"executing_tool"}\n\n'
        entered.set()
        await tmux_harness.run_async(
            "sleep 30",
            tmp_path,
            session_id=session_id,
            timeout=30,
        )

    run = agent_runs.start(session_id, command_stream())
    subscriber = agent_runs.subscribe(session_id)
    await subscriber.__anext__()
    await asyncio.wait_for(entered.wait(), timeout=1)
    await subscriber.aclose()
    await asyncio.sleep(0.05)
    assert run.status == "running"

    started = time.monotonic()
    assert agent_runs.stop(session_id) is True
    await asyncio.wait_for(run.task, timeout=3)

    assert time.monotonic() - started < 3
    assert run.status == "stopped"
    recovered = await tmux_harness.run_async(
        "printf recovered",
        tmp_path,
        session_id=session_id,
    )
    assert recovered.stdout == "recovered"
    agent_runs._RUNS.pop(session_id, None)


@pytest.mark.asyncio
async def test_background_marker_bypasses_foreground_watchdog(
    monkeypatch,
    tmp_path,
):
    launched = []
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr(
        bg_jobs,
        "launch",
        lambda command, **kwargs: (
            launched.append((command, kwargs))
            or {"id": "bg-test", "status": "running"}
        ),
    )

    description, result = await tool_execution.execute_tool_block(
        ToolBlock("bash", "#!bg\nsleep 30"),
        session_id="background-contract",
        owner="admin",
        workspace=str(tmp_path),
        allowed_tools={"bash"},
        execution_mode="sandboxed",
    )

    assert description == "run_sandbox_command: success"
    assert result["job_id"] == "bg-test"
    assert result["exit_code"] == 0
    assert len(launched) == 1
    command, launch = launched[0]
    assert command == "sleep 30"
    assert launch["session_id"] == "background-contract"
    assert launch["cwd"] == str(tmp_path)
    assert launch["owner"] == "admin"
    assert launch["execution_mode"] == "sandboxed"
    assert launch["execution_context"].execution_root.path == str(tmp_path)


def test_frontend_has_timeout_and_terminal_cleanup_contract():
    source = Path("static/js/chat.js").read_text(encoding="utf-8")

    assert "node.dataset.invocationId" in source
    assert "json.timeout_seconds" in source
    assert "toolTerminalState(json)" in source
    assert "settleRunningToolNodes(document, 'cancelled')" in source
    assert "runtimeStateToolStatus(_runtimeEvents.runState)" in source
    reducer = Path("static/js/runtimeEvents.js").read_text(encoding="utf-8")
    assert "raw.type === 'tool_result'" in reducer
    assert "result.status === 'timed_out'" in reducer
