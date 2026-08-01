"""Security boundary for model-authored local subprocesses."""

import os
import shutil

import pytest

from src.agent_tools.subprocess_tools import _run_direct_bash
from src.execution_policy import ExecutionMode
from src.process_sandbox import SandboxUnavailable, minimal_environment, sandbox_command


def test_minimal_environment_does_not_forward_undeclared_secrets():
    environment = minimal_environment(
        {
            "LANG": "C.UTF-8",
            "PATH": "/host/custom/bin",
            "DATABASE_URL": "postgres://secret",
            "OPENAI_API_KEY": "secret",
            "AWS_SECRET_ACCESS_KEY": "secret",
        }
    )

    assert environment["LANG"] == "C.UTF-8"
    assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert environment["HOME"] == "/tmp/odysseus-home"
    assert "DATABASE_URL" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment


def test_sandbox_command_has_fail_closed_namespace_and_resource_limits(tmp_path):
    if not shutil.which("bwrap") or not shutil.which("prlimit"):
        pytest.skip("Linux bubblewrap sandbox is not installed")

    argv, environment = sandbox_command(
        ["/bin/bash", "-c", "true"],
        cwd=tmp_path,
        environment={"OPENAI_API_KEY": "must-not-leak"},
        timeout_seconds=10,
    )

    assert "--unshare-all" in argv
    assert "--share-net" not in argv
    assert ["--ro-bind", "/", "/"] == argv[
        argv.index("--ro-bind") : argv.index("--ro-bind") + 3
    ]
    assert "--cap-drop" in argv
    assert any(part.startswith("--cpu=") for part in argv)
    assert any(part.startswith("--as=") for part in argv)
    assert "OPENAI_API_KEY" not in environment


def test_application_data_root_cannot_be_selected_as_safe_shell_workspace():
    if not shutil.which("bwrap") or not shutil.which("prlimit"):
        pytest.skip("Linux bubblewrap sandbox is not installed")
    from src.constants import DATA_DIR

    with pytest.raises(SandboxUnavailable, match="application-data root"):
        sandbox_command(
            ["/bin/true"],
            cwd=DATA_DIR,
            timeout_seconds=5,
        )


@pytest.mark.asyncio
async def test_bash_cannot_see_secret_or_write_outside_workspace(tmp_path):
    if not shutil.which("bwrap") or not shutil.which("prlimit"):
        pytest.skip("Linux bubblewrap sandbox is not installed")

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    escaped = outside / "escaped"
    result = await _run_direct_bash(
        (
            'printf "%s" "${AUDIT_SECRET-unset}"; '
            f'if touch {escaped!s} 2>/dev/null; then printf ":escaped"; '
            'else printf ":confined"; fi'
        ),
        session_id="sandbox-regression",
        cwd=str(workspace),
        env={"AUDIT_SECRET": "must-not-leak", "LANG": "C"},
        timeout=10,
        progress_cb=None,
        invocation_id="sandbox-test-1234",
        execution_mode=ExecutionMode.SANDBOXED,
    )

    assert result.exit_code == 0
    assert result.stdout == "unset:confined"
    assert not escaped.exists()


@pytest.mark.asyncio
async def test_host_cwd_state_cannot_rebind_later_sandbox_outside_workspace(tmp_path):
    if not shutil.which("bwrap") or not shutil.which("prlimit"):
        pytest.skip("Linux bubblewrap sandbox is not installed")

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    session = "cross-mode-cwd-regression"

    host = await _run_direct_bash(
        "cd ../outside; pwd",
        session_id=session,
        cwd=str(workspace),
        env={},
        timeout=10,
        progress_cb=None,
        invocation_id="host-cwd-regression-1234",
        execution_mode=ExecutionMode.HOST,
    )
    assert host.stdout.strip() == str(outside)

    sandbox = await _run_direct_bash(
        "pwd; cd ..; touch sandbox-escaped 2>/dev/null || true",
        session_id=session,
        cwd=str(workspace),
        env={},
        timeout=10,
        progress_cb=None,
        invocation_id="sandbox-cwd-regression-1234",
        execution_mode=ExecutionMode.SANDBOXED,
    )
    assert sandbox.stdout.splitlines()[0] == str(workspace)
    assert not (tmp_path / "sandbox-escaped").exists()

    sandbox_again = await _run_direct_bash(
        "pwd",
        session_id=session,
        cwd=str(workspace),
        env={},
        timeout=10,
        progress_cb=None,
        invocation_id="sandbox-cwd-regression-5678",
        execution_mode=ExecutionMode.SANDBOXED,
    )
    assert sandbox_again.stdout.strip() == str(workspace)
