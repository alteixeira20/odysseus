"""Single process boundary for migrated shell and Python execution."""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
import time
from typing import Any, Awaitable, Callable, Mapping, Optional

from src.agent_tools.subprocess_tools import (
    DEFAULT_PYTHON_TIMEOUT,
    _run_direct_bash,
    _run_subprocess_streaming,
    normalize_bash_timeout,
)
from src.constants import MAX_OUTPUT_CHARS
from src.execution_policy import ExecutionMode
from src.process_sandbox import SandboxUnavailable, minimal_environment, sandbox_command
from src.tool_utils import _truncate

from .contracts import AgentExecutionContext


class ProcessServiceError(RuntimeError):
    code = "process_error"


class ProcessSandboxUnavailable(ProcessServiceError):
    code = "sandbox_unavailable"


class ProcessService:
    """Owns target selection, roots, environment, limits, and termination."""

    def run_workspace_search(
        self,
        argv: list[str],
        *,
        root: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        """Run a service-owned, argument-vector workspace search process."""

        canonical_root = os.path.realpath(root)
        if not os.path.isabs(canonical_root) or not os.path.isdir(canonical_root):
            raise ProcessServiceError("workspace search requires an explicit root")
        completed = subprocess.run(
            list(argv),
            cwd=canonical_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=minimal_environment({}),
            timeout=max(float(timeout_seconds), 1.0),
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    async def run_command(
        self,
        arguments: Mapping[str, Any],
        context: AgentExecutionContext,
        *,
        progress_cb: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
        invocation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        context.cancellation_token.raise_if_cancelled()
        if context.execution_mode not in {ExecutionMode.SANDBOXED, ExecutionMode.HOST}:
            raise ProcessServiceError("process execution is disabled for this run")
        command = str(arguments.get("command") or "")
        if not command.strip():
            raise ProcessServiceError("command may not be empty")
        timeout = min(
            normalize_bash_timeout(arguments.get("timeout_seconds")),
            max(float(context.budgets.wall_clock_seconds), 1.0),
        )
        call_id = str(invocation_id or secrets.token_urlsafe(24))
        environment = (
            None
            if context.execution_mode is ExecutionMode.HOST
            else minimal_environment({})
        )
        try:
            outcome = await _run_direct_bash(
                command,
                session_id=context.session_id,
                cwd=context.execution_root.path,
                env=environment,
                timeout=timeout,
                progress_cb=progress_cb,
                invocation_id=call_id,
                execution_mode=context.execution_mode,
                preserve_logical_cwd=False,
            )
        except SandboxUnavailable as exc:
            raise ProcessSandboxUnavailable(str(exc)) from exc
        raw_stdout = outcome.stdout or ""
        raw_stderr = outcome.stderr or ""
        output_limit = max(
            1,
            min(int(context.budgets.max_output_chars), MAX_OUTPUT_CHARS),
        )
        stdout = _truncate(raw_stdout, output_limit)
        stderr = _truncate(raw_stderr, output_limit)
        text = stdout.rstrip()
        if stderr.rstrip():
            text = (
                (text + "\nSTDERR: " + stderr.rstrip()).strip()
                if text
                else "STDERR: " + stderr.rstrip()
            )
        return {
            "text": text or "(no output)",
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": outcome.exit_code,
            "timed_out": outcome.timed_out,
            "timeout_seconds": outcome.timeout_seconds,
            "execution_mode": context.execution_mode.value,
            "execution_root": context.execution_root.path,
            "escalation": list(outcome.escalation),
            "process_recovery": outcome.session_recovery,
            "truncation": {
                "stdout": len(raw_stdout) > output_limit,
                "stderr": len(raw_stderr) > output_limit,
                "stdout_total_chars": len(raw_stdout),
                "stderr_total_chars": len(raw_stderr),
            },
            "backend": (
                "bubblewrap"
                if context.execution_mode is ExecutionMode.SANDBOXED
                else "host"
            ),
        }

    async def run_python(
        self,
        arguments: Mapping[str, Any],
        context: AgentExecutionContext,
        *,
        progress_cb: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
    ) -> dict[str, Any]:
        context.cancellation_token.raise_if_cancelled()
        if context.execution_mode not in {ExecutionMode.SANDBOXED, ExecutionMode.HOST}:
            raise ProcessServiceError("Python execution is disabled for this run")
        code = str(arguments.get("code") or "")
        if not code.strip():
            raise ProcessServiceError("Python code may not be empty")
        try:
            timeout = float(arguments.get("timeout_seconds") or DEFAULT_PYTHON_TIMEOUT)
        except (TypeError, ValueError):
            timeout = float(DEFAULT_PYTHON_TIMEOUT)
        timeout = min(
            max(timeout, 1.0),
            float(DEFAULT_PYTHON_TIMEOUT),
            max(float(context.budgets.wall_clock_seconds), 1.0),
        )
        python_executable = os.path.realpath(sys.executable or "python")
        try:
            if context.execution_mode is ExecutionMode.HOST:
                argv = [python_executable, "-I", "-c", code]
                environment = os.environ.copy()
            else:
                argv, environment = sandbox_command(
                    [python_executable, "-I", "-c", code],
                    cwd=context.execution_root.path,
                    environment=minimal_environment({}),
                    timeout_seconds=timeout,
                )
        except SandboxUnavailable as exc:
            raise ProcessSandboxUnavailable(str(exc)) from exc
        process_kwargs: dict[str, Any] = {}
        if os.name == "posix":
            process_kwargs["start_new_session"] = True
        elif os.name == "nt":
            process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        started = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            cwd=context.execution_root.path,
            **process_kwargs,
        )
        stdout, stderr, return_code, timed_out, escalation = await _run_subprocess_streaming(
            proc,
            timeout=timeout,
            progress_cb=progress_cb,
            terminate_process_group=True,
        )
        raw_stdout = stdout or ""
        raw_stderr = stderr or ""
        output_limit = max(
            1,
            min(int(context.budgets.max_output_chars), MAX_OUTPUT_CHARS),
        )
        bounded_stdout = _truncate(raw_stdout, output_limit)
        bounded_stderr = _truncate(raw_stderr, output_limit)
        text = bounded_stdout.rstrip()
        if bounded_stderr.rstrip():
            text = (
                (text + "\nSTDERR: " + bounded_stderr.rstrip()).strip()
                if text
                else "STDERR: " + bounded_stderr.rstrip()
            )
        return {
            "text": text or "(no output)",
            "stdout": bounded_stdout,
            "stderr": bounded_stderr,
            "exit_code": 124 if timed_out else (return_code if return_code is not None else 1),
            "timed_out": timed_out,
            "timeout_seconds": timeout,
            "execution_mode": context.execution_mode.value,
            "execution_root": context.execution_root.path,
            "escalation": list(escalation),
            "duration_ms": (time.perf_counter() - started) * 1000,
            "truncation": {
                "stdout": len(raw_stdout) > output_limit,
                "stderr": len(raw_stderr) > output_limit,
                "stdout_total_chars": len(raw_stdout),
                "stderr_total_chars": len(raw_stderr),
            },
            "backend": (
                "bubblewrap_python"
                if context.execution_mode is ExecutionMode.SANDBOXED
                else "host_python"
            ),
        }

    def launch_background(
        self,
        command: str,
        context: AgentExecutionContext,
    ) -> dict[str, Any]:
        from src import bg_jobs

        context.cancellation_token.raise_if_cancelled()
        return bg_jobs.launch(
            command,
            session_id=context.session_id,
            cwd=context.execution_root.path,
            owner=context.owner_id,
            execution_mode=context.execution_mode.value,
            execution_context=context,
        )


PROCESS_SERVICE = ProcessService()
