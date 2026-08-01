"""Single process boundary for migrated shell and Python execution."""

from __future__ import annotations

import asyncio
import os
import secrets
import selectors
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
from src.process_sandbox import (
    SandboxUnavailable,
    host_command_boundary,
    minimal_environment,
    redact_sensitive_output,
    sandbox_command,
)
from src.tool_utils import _truncate

from .contracts import AgentExecutionContext, Capability
from .workspace_service import WORKSPACE_SERVICE


class ProcessServiceError(RuntimeError):
    code = "process_error"


class ProcessSandboxUnavailable(ProcessServiceError):
    code = "sandbox_unavailable"


class ProcessService:
    """Owns target selection, roots, environment, limits, and termination."""

    @staticmethod
    def _record_workspace_transition(
        context: AgentExecutionContext,
        before_revision: str,
    ) -> tuple[str, bool]:
        observed = WORKSPACE_SERVICE.revision(context.execution_root.path)
        if observed == before_revision:
            return observed, False
        after = WORKSPACE_SERVICE.note_external_mutation(
            context.execution_root.path
        )
        if not context.authority_grant.allows(Capability.WORKSPACE_WRITE):
            raise ProcessServiceError(
                "read-only process boundary detected an unauthorized workspace mutation"
            )
        return after, True

    def run_workspace_search(
        self,
        argv: list[str],
        *,
        root: str,
        timeout_seconds: float,
        max_output_bytes: int = 1_048_576,
    ) -> dict[str, Any]:
        """Run a service-owned, argument-vector workspace search process."""

        canonical_root = os.path.realpath(root)
        if not os.path.isabs(canonical_root) or not os.path.isdir(canonical_root):
            raise ProcessServiceError("workspace search requires an explicit root")
        process = subprocess.Popen(
            list(argv),
            cwd=canonical_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=minimal_environment({}),
        )
        stdout = bytearray()
        stderr = bytearray()
        budget_exhausted = False
        timed_out = False
        deadline = time.monotonic() + max(float(timeout_seconds), 1.0)
        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, stdout)
        selector.register(process.stderr, selectors.EVENT_READ, stderr)
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    process.terminate()
                    break
                ready = selector.select(min(remaining, 0.25))
                if not ready and process.poll() is not None:
                    break
                for key, _ in ready:
                    chunk = os.read(key.fileobj.fileno(), 65_536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = key.data
                    remaining_bytes = max_output_bytes - len(target)
                    if remaining_bytes > 0:
                        target.extend(chunk[:remaining_bytes])
                    if target is stdout and len(chunk) > remaining_bytes:
                        budget_exhausted = True
                        process.terminate()
                        break
                if budget_exhausted:
                    break
        finally:
            selector.close()
        try:
            returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            returncode = process.wait(timeout=2)
        return {
            "returncode": returncode,
            "stdout": bytes(stdout),
            "stderr": bytes(stderr),
            "budget_exhausted": budget_exhausted,
            "timed_out": timed_out,
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
        workspace_revision_before = WORKSPACE_SERVICE.revision(
            context.execution_root.path
        )
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
                workspace_writable=context.authority_grant.allows(
                    Capability.WORKSPACE_WRITE
                ),
            )
        except SandboxUnavailable as exc:
            raise ProcessSandboxUnavailable(str(exc)) from exc
        workspace_revision, workspace_mutated = self._record_workspace_transition(
            context,
            workspace_revision_before,
        )
        raw_stdout, stdout_redactions = redact_sensitive_output(outcome.stdout or "")
        raw_stderr, stderr_redactions = redact_sensitive_output(outcome.stderr or "")
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
            "workspace_access": (
                "read_write"
                if context.authority_grant.allows(Capability.WORKSPACE_WRITE)
                else "read_only"
            ),
            "workspace_view_policy": (
                "tracked_untracked_ignored_read_write_with_common_secrets_masked"
                if context.authority_grant.allows(Capability.WORKSPACE_WRITE)
                else "tracked_untracked_ignored_read_only_with_common_secrets_masked"
            ),
            "workspace_revision_before": workspace_revision_before,
            "workspace_revision": workspace_revision,
            "workspace_mutated": workspace_mutated,
            "workspace_snapshot_policy": (
                "git_head_status_with_bounded_dirty_and_untracked_content; "
                "ignored_dependency_artifacts_outside_revision"
            ),
            "workspace_boundary": (
                "sandbox_bubblewrap"
                if context.execution_mode is ExecutionMode.SANDBOXED
                else (
                    "direct_host_workspace_write"
                    if context.authority_grant.allows(Capability.WORKSPACE_WRITE)
                    else "host_bubblewrap_read_only_overlay"
                )
            ),
            "output_redactions": stdout_redactions + stderr_redactions,
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
        workspace_revision_before = WORKSPACE_SERVICE.revision(
            context.execution_root.path
        )
        try:
            if context.execution_mode is ExecutionMode.HOST:
                argv, environment = host_command_boundary(
                    [python_executable, "-I", "-c", code],
                    cwd=context.execution_root.path,
                    workspace_writable=context.authority_grant.allows(
                        Capability.WORKSPACE_WRITE
                    ),
                    environment=os.environ.copy(),
                )
            else:
                argv, environment = sandbox_command(
                    [python_executable, "-I", "-c", code],
                    cwd=context.execution_root.path,
                    environment=minimal_environment({}),
                    timeout_seconds=timeout,
                    writable_paths=(
                        (context.execution_root.path,)
                        if context.authority_grant.allows(Capability.WORKSPACE_WRITE)
                        else ()
                    ),
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
        workspace_revision, workspace_mutated = self._record_workspace_transition(
            context,
            workspace_revision_before,
        )
        raw_stdout, stdout_redactions = redact_sensitive_output(stdout or "")
        raw_stderr, stderr_redactions = redact_sensitive_output(stderr or "")
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
            "workspace_access": (
                "read_write"
                if context.authority_grant.allows(Capability.WORKSPACE_WRITE)
                else "read_only"
            ),
            "workspace_view_policy": (
                "tracked_untracked_ignored_read_write_with_common_secrets_masked"
                if context.authority_grant.allows(Capability.WORKSPACE_WRITE)
                else "tracked_untracked_ignored_read_only_with_common_secrets_masked"
            ),
            "workspace_revision_before": workspace_revision_before,
            "workspace_revision": workspace_revision,
            "workspace_mutated": workspace_mutated,
            "workspace_snapshot_policy": (
                "git_head_status_with_bounded_dirty_and_untracked_content; "
                "ignored_dependency_artifacts_outside_revision"
            ),
            "workspace_boundary": (
                "sandbox_bubblewrap"
                if context.execution_mode is ExecutionMode.SANDBOXED
                else (
                    "direct_host_workspace_write"
                    if context.authority_grant.allows(Capability.WORKSPACE_WRITE)
                    else "host_bubblewrap_read_only_overlay"
                )
            ),
            "output_redactions": stdout_redactions + stderr_redactions,
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
