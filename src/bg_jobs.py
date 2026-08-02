"""Background job execution for the agent's `bash` tool.

Long commands (installs, ffmpeg, model downloads) should NOT block the chat
stream — a multi-minute held SSE connection is fragile (model-stops-early,
timeouts, tab suspend). Instead we launch them **detached** and let an
always-on monitor re-invoke the agent when they finish ("auto-continue").

Design goals:
  * Restart-safe: status is derived from an on-disk exit-code file, not a live
    PID, so a uvicorn restart never loses a job or its result.
  * Idempotent follow-up: a job stays {done, followed_up: False} until the
    agent has actually been re-invoked, so completion can never silently
    "do nothing" — the monitor retries on the next tick.
  * Bounded: a hard max-runtime marks a runaway job failed and STILL triggers
    a follow-up ("timed out"), so you always hear back.

This module only owns launch + state. The monitor / agent re-invocation lives
in the caller (so this stays import-light and unit-testable).
"""

from __future__ import annotations

import json
import os
import base64
import shlex
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from core.platform_compat import (
    detached_popen_kwargs,
    find_bash,
    git_bash_path,
    kill_process_tree,
    pid_alive,
)

from src.constants import BG_JOBS_DIR, BG_JOBS_FILE
from src.execution_policy import ExecutionMode, normalize_execution_mode
from src.process_sandbox import (
    host_command_boundary,
    minimal_environment,
    redact_sensitive_output,
    sandbox_command,
)

_JOBS_DIR = Path(BG_JOBS_DIR)
_STORE = Path(BG_JOBS_FILE)

# A job that runs longer than this is presumed stuck and reaped (the agent
# still gets a "timed out" follow-up so nothing hangs forever).
DEFAULT_MAX_RUNTIME_S = 3600  # 1 hour
# Cap how much captured output we keep / feed back to the model.
_MAX_OUTPUT_CHARS = 16000
# How long a finished-and-followed-up job (record + its .sh/.cmd.sh/.log/.exit
# files) is kept before pruning, so neither the store nor data/bg_jobs/ grows
# without bound. The agent has already consumed the result by then.
_RETENTION_S = 3600  # 1 hour after follow-up
MAX_RUNNING_JOBS_GLOBAL = 16
MAX_RUNNING_JOBS_PER_OWNER = 8
MAX_RUNNING_JOBS_PER_SESSION = 4


def _terminate_spawned_process(proc: subprocess.Popen) -> None:
    """Synchronously terminate and reap a just-created detached process."""

    kill_process_tree(proc.pid)
    try:
        proc.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=2)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        pass


def _load() -> Dict[str, Dict[str, Any]]:
    try:
        if _STORE.exists():
            data = json.loads(_STORE.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                return {}
            return {str(job_id): rec for job_id, rec in data.items() if isinstance(rec, dict)}
    except Exception:
        pass
    return {}


def _save(jobs: Dict[str, Dict[str, Any]]) -> None:
    atomic_write_json(str(_STORE), jobs, indent=2)


def _pid_alive(pid: Optional[int]) -> bool:
    # Delegates to the platform-safe probe. NB: a bare os.kill(pid, 0) is unsafe
    # on Windows — CPython routes it to TerminateProcess, which would KILL the
    # job we're only trying to check. core.platform_compat.pid_alive handles
    # both OSes correctly.
    return pid_alive(pid)


def launch(
    command: str,
    session_id: str,
    cwd: Optional[str] = None,
    max_runtime_s: int = DEFAULT_MAX_RUNTIME_S,
    *,
    owner: Optional[str] = None,
    execution_mode: str = "disabled",
    execution_context: Optional[Any] = None,
    effect_started_cb: Optional[Any] = None,
    spawn_started_cb: Optional[Any] = None,
    spawn_failed_cb: Optional[Any] = None,
    approval_id: Optional[str] = None,
    approval_identity: Optional[str] = None,
) -> Dict[str, Any]:
    """Launch `command` detached. Returns the job record (status='running').

    Output + the final exit code are written to files so status survives a
    server restart. The process is put in its own session (setsid) so it
    outlives the request/stream that started it.
    """
    context_snapshot: Dict[str, Any] = {}
    process_workspace_write_granted = False
    if execution_context is not None:
        from src.agent.runtime_v2.contracts import Capability
        from src.agent.runtime_v2.workspace_service import WORKSPACE_SERVICE

        if str(session_id) != str(execution_context.session_id):
            raise ValueError("background session does not match execution context")
        if str(owner or "") != str(execution_context.owner_id):
            raise ValueError("background owner does not match execution context")
        canonical_cwd = os.path.realpath(str(cwd or ""))
        if canonical_cwd != execution_context.execution_root.path:
            raise ValueError("background root does not match execution context")
        mode = execution_context.execution_mode
        process_workspace_write_granted = execution_context.authority_grant.allows(
            Capability.PROCESS_WORKSPACE_WRITE
        )
        max_runtime_s = max(
            1,
            min(
                int(max_runtime_s),
                int(execution_context.budgets.wall_clock_seconds),
            ),
        )
        context_snapshot = {
            "run_id": execution_context.run_id,
            "conversation_id": execution_context.conversation_id,
            "turn_id": execution_context.turn_id,
            "candidate_id": execution_context.candidate_id,
            "execution_target": mode.value,
            "execution_root": execution_context.execution_root.path,
            "workspace_revision": WORKSPACE_SERVICE.revision(
                execution_context.execution_root.path
            ),
            "authority_revision": execution_context.authority_grant.revision,
            "process_workspace_write_granted": process_workspace_write_granted,
            "resource_limits": {
                "wall_clock_seconds": execution_context.budgets.wall_clock_seconds,
                "idle_seconds": execution_context.budgets.idle_seconds,
                "max_output_chars": execution_context.budgets.max_output_chars,
            },
            "cancellation_identity": execution_context.cancellation_token.identity,
            "bounded_output_chars": _MAX_OUTPUT_CHARS,
        }
    else:
        mode = normalize_execution_mode(execution_mode)
    if not mode.enabled:
        raise RuntimeError("background execution has no process authority")
    active_jobs = [
        record for record in refresh().values()
        if record.get("status") == "running"
    ]
    if len(active_jobs) >= MAX_RUNNING_JOBS_GLOBAL:
        raise RuntimeError("server background-job limit reached")
    if sum(record.get("session_id") == session_id for record in active_jobs) >= MAX_RUNNING_JOBS_PER_SESSION:
        raise RuntimeError("session background-job limit reached")
    if owner and sum(record.get("owner") == owner for record in active_jobs) >= MAX_RUNNING_JOBS_PER_OWNER:
        raise RuntimeError("owner background-job limit reached")

    _JOBS_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]
    log_path = _JOBS_DIR / f"{job_id}.log"
    exit_path = _JOBS_DIR / f"{job_id}.exit"

    if not cwd or not os.path.isdir(os.path.realpath(cwd)):
        raise ValueError("background execution requires an explicit workspace")

    bash = find_bash()
    if not bash:
        raise RuntimeError("background Bash requires an installed Bash executable")

    if mode is ExecutionMode.SANDBOXED and os.name == "posix":
        # Only this fixed wrapper runs on the host. The model-authored command
        # is encoded into Bubblewrap and inherits the foreground sandbox.
        encoded = base64.b64encode(command.encode("utf-8", errors="surrogatepass")).decode("ascii")
        sandbox_argv, _sandbox_env = sandbox_command(
            [
                "/bin/bash", "--noprofile", "--norc", "-c",
                'eval "$(printf "%s" "$ODY_COMMAND_B64" | base64 -d)"',
            ],
            cwd=os.path.realpath(cwd),
            environment={},
            runtime_environment={"ODY_COMMAND_B64": encoded},
            timeout_seconds=max_runtime_s,
            writable_paths=(
                (os.path.realpath(cwd),)
                if process_workspace_write_granted
                else ()
            ),
        )
        lp, xp = (shlex.quote(git_bash_path(p)) for p in (log_path, exit_path))
        script_path = _JOBS_DIR / f"{job_id}.sh"
        script_path.write_text(
            f"{shlex.join(sandbox_argv)} > {lp} 2>&1\n"
            f"printf '%s' $? > {xp}\n",
            encoding="utf-8",
        )
        argv = [bash, str(script_path)]
        popen_stdout = subprocess.DEVNULL
        popen_stderr = subprocess.DEVNULL
        child_environment = minimal_environment({})
    elif mode is ExecutionMode.HOST:
        # Full host mode retains host visibility/network, but executes with the
        # same exact, minimal environment that was bound into approval. A trap
        # persists the exit status even when the command itself calls `exit`.
        from src.agent.runtime_v2.process_identity import execution_environment

        encoded = base64.b64encode(command.encode("utf-8", errors="surrogatepass")).decode("ascii")
        exit_target = git_bash_path(exit_path)
        script_path = _JOBS_DIR / f"{job_id}.sh"
        script_path.write_text(
            "__ody_finish() { __ody_rc=$?; "
            f"printf '%s' \"$__ody_rc\" > {shlex.quote(exit_target)}; }}\n"
            "trap __ody_finish EXIT\n"
            f"ODY_COMMAND_B64={shlex.quote(encoded)}\n"
            'eval "$(printf "%s" "$ODY_COMMAND_B64" | base64 -d)"\n',
            encoding="utf-8",
        )
        argv, child_environment = host_command_boundary(
            [bash, str(script_path)],
            cwd=os.path.realpath(cwd),
            workspace_writable=process_workspace_write_granted,
            environment=execution_environment(),
        )
        log_handle = log_path.open("ab")
        popen_stdout = log_handle
        popen_stderr = subprocess.STDOUT
    else:
        raise RuntimeError(
            "Safe workspace background shell requires Linux Bubblewrap; "
            "use an explicitly authorized Full host shell on this platform"
        )

    try:
        if spawn_started_cb is not None:
            try:
                spawn_started_cb()
            except BaseException:
                if spawn_failed_cb is not None:
                    spawn_failed_cb()
                raise
        try:
            proc = subprocess.Popen(
                argv,
                stdout=popen_stdout,
                stderr=popen_stderr,
                stdin=subprocess.DEVNULL,
                cwd=cwd or None,
                env=child_environment,
                **detached_popen_kwargs(),  # setsid / DETACHED_PROCESS
            )
        except BaseException:
            if spawn_failed_cb is not None:
                spawn_failed_cb()
            raise
        if effect_started_cb is not None:
            try:
                effect_started_cb()
            except BaseException:
                _terminate_spawned_process(proc)
                raise
    finally:
        if mode is ExecutionMode.HOST:
            log_handle.close()

    started_at = time.time()
    rec = {
        "id": job_id,
        "session_id": session_id,
        "owner": owner,
        "execution_mode": mode.value,
        "command": redact_sensitive_output(command)[0],
        "status": "running",       # running | done | failed
        "pid": proc.pid,
        "started_at": started_at,
        "deadline": started_at + max_runtime_s,
        "ended_at": None,
        "exit_code": None,
        "max_runtime_s": max_runtime_s,
        "followed_up": False,       # has the agent been re-invoked with the result?
        "approval_id": approval_id,
        "approval_identity": approval_identity,
        "approval_binding": "exact_opaque_command",
        "complete_dependency_seal": False,
        "detached_outcome": "detached_executing",
        "log_path": str(log_path),
        "exit_path": str(exit_path),
        **context_snapshot,
    }
    jobs = _load()
    jobs[job_id] = rec
    _save(jobs)
    return rec


def _read_output(rec: Dict[str, Any]) -> str:
    try:
        txt = Path(rec["log_path"]).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    txt, _ = redact_sensitive_output(txt)
    if len(txt) > _MAX_OUTPUT_CHARS:
        # Keep head + tail — the interesting bits are usually at both ends.
        head = txt[: _MAX_OUTPUT_CHARS // 2]
        tail = txt[-_MAX_OUTPUT_CHARS // 2:]
        txt = head + "\n…[truncated]…\n" + tail
    return txt


def _prune(jobs: Dict[str, Dict[str, Any]], now: float) -> bool:
    """Drop records (and their on-disk files) for jobs that finished, were
    followed up, and are older than the retention window. Mutates `jobs`."""
    stale = [jid for jid, rec in jobs.items()
             if rec.get("followed_up") and rec.get("ended_at")
             and (now - rec["ended_at"]) > _RETENTION_S]
    for jid in stale:
        jobs.pop(jid, None)
        for p in _JOBS_DIR.glob(f"{jid}.*"):   # .sh .cmd.sh .log .exit
            try:
                p.unlink()
            except Exception:
                pass
    return bool(stale)


def refresh() -> Dict[str, Dict[str, Any]]:
    """Reconcile every running job against disk. Marks done/failed (incl.
    timeout). Idempotent — safe to call from a poll loop. Returns the store."""
    jobs = _load()
    changed = False
    now = time.time()
    for rec in jobs.values():
        if rec.get("status") != "running":
            continue
        exit_path = Path(rec.get("exit_path", ""))
        completed_now = False
        if exit_path.exists():
            try:
                code = int(exit_path.read_text(encoding="utf-8", errors="replace").strip() or "1")
            except Exception:
                code = 1
            rec["exit_code"] = code
            rec["status"] = "done" if code == 0 else "failed"
            rec["ended_at"] = now
            changed = True
            completed_now = True
        elif (now - rec.get("started_at", now)) > rec.get("max_runtime_s", DEFAULT_MAX_RUNTIME_S):
            # Runaway / stuck — reap it but STILL surface a follow-up.
            _kill(rec.get("pid"))
            rec["status"] = "failed"
            rec["exit_code"] = -1
            rec["ended_at"] = now
            rec["timed_out"] = True
            changed = True
            completed_now = True
        elif not _pid_alive(rec.get("pid")) and not exit_path.exists():
            # Process vanished without writing an exit code (killed, OOM,
            # crash). Don't leave it "running" forever.
            rec["status"] = "failed"
            rec["exit_code"] = -1
            rec["ended_at"] = now
            rec["died"] = True
            changed = True
            completed_now = True
        if completed_now and rec.get("execution_root") and rec.get("workspace_revision"):
            try:
                from src.agent.runtime_v2.workspace_service import WORKSPACE_SERVICE

                observed = WORKSPACE_SERVICE.revision(
                    rec["execution_root"], force=True
                )
                if observed != rec["workspace_revision"]:
                    rec["workspace_revision_after"] = WORKSPACE_SERVICE.note_external_mutation(
                        rec["execution_root"], observed_revision=observed
                    )
                    rec["workspace_mutated"] = True
                    if not rec.get(
                        "process_workspace_write_granted",
                        rec.get("workspace_write_granted", False),
                    ):
                        rec["status"] = "failed"
                        rec["boundary_violation"] = True
                        rec["exit_code"] = -1
            except Exception:
                rec["workspace_revision_unavailable"] = True
        if completed_now:
            if rec.get("timed_out"):
                detached_outcome = "detached_timed_out"
            elif rec.get("cancelled_with_run") or rec.get("killed"):
                detached_outcome = "detached_cancelled"
            elif rec.get("status") == "done" and rec.get("exit_code") == 0:
                detached_outcome = "detached_completed"
            else:
                detached_outcome = "detached_failed"
            rec["detached_outcome"] = detached_outcome
            approval_id = rec.get("approval_id")
            if approval_id:
                try:
                    from src.agent.runtime_v2.approvals import EFFECT_APPROVALS

                    EFFECT_APPROVALS.finalize_detached(approval_id, detached_outcome)
                except Exception:
                    rec["approval_finalization_unavailable"] = True
    if _prune(jobs, now):
        changed = True
    if changed:
        _save(jobs)
    return jobs


def _kill(pid: Optional[int]) -> None:
    # Cross-platform process-tree teardown (POSIX killpg / Windows taskkill /T).
    kill_process_tree(pid)


def pending_followups() -> List[Dict[str, Any]]:
    """Finished jobs the agent hasn't been re-invoked for yet. The monitor
    drains these; mark_followed_up() flips the flag only on success."""
    jobs = refresh()
    return [r for r in jobs.values()
            if r.get("status") in ("done", "failed") and not r.get("followed_up")]


def mark_followed_up(job_id: str) -> None:
    jobs = _load()
    if job_id in jobs:
        jobs[job_id]["followed_up"] = True
        _save(jobs)


def get(job_id: str) -> Optional[Dict[str, Any]]:
    refresh()  # reconcile against disk so status/exit_code are current
    rec = _load().get(job_id)
    if rec:
        rec = dict(rec)
        rec["output"] = _read_output(rec)
    return rec


def list_for_session(session_id: str) -> List[Dict[str, Any]]:
    return [r for r in refresh().values() if r.get("session_id") == session_id]


def kill_for_session_since(session_id: str, started_at: float) -> int:
    """Kill jobs launched by a cancelled run, leaving older chat jobs alone."""
    jobs = _load()
    killed = 0
    for rec in jobs.values():
        if (
            rec.get("session_id") != session_id
            or rec.get("status") != "running"
            or float(rec.get("started_at") or 0) < float(started_at or 0)
        ):
            continue
        _kill(rec.get("pid"))
        rec["status"] = "failed"
        rec["exit_code"] = -1
        rec["ended_at"] = time.time()
        rec["killed"] = True
        rec["cancelled_with_run"] = True
        rec["detached_outcome"] = "detached_cancelled"
        rec["followed_up"] = True
        killed += 1
        if rec.get("approval_id"):
            try:
                from src.agent.runtime_v2.approvals import EFFECT_APPROVALS

                EFFECT_APPROVALS.finalize_detached(
                    rec["approval_id"], "detached_cancelled"
                )
            except Exception:
                rec["approval_finalization_unavailable"] = True
    if killed:
        _save(jobs)
    return killed


def kill(job_id: str) -> Optional[Dict[str, Any]]:
    """Terminate a running job's process tree and mark it killed. Returns the
    updated record, or None if the id is unknown. Idempotent: a job that already
    finished is returned unchanged. Sets followed_up so the monitor does not also
    fire an auto-continue for a job the agent deliberately stopped."""
    jobs = _load()
    rec = jobs.get(job_id)
    if rec is None:
        return None
    if rec.get("status") == "running":
        _kill(rec.get("pid"))
        rec["status"] = "failed"
        rec["exit_code"] = -1
        rec["ended_at"] = time.time()
        rec["killed"] = True
        rec["detached_outcome"] = "detached_cancelled"
        rec["followed_up"] = True
        if rec.get("approval_id"):
            try:
                from src.agent.runtime_v2.approvals import EFFECT_APPROVALS

                EFFECT_APPROVALS.finalize_detached(
                    rec["approval_id"], "detached_cancelled"
                )
            except Exception:
                rec["approval_finalization_unavailable"] = True
        _save(jobs)
    return rec


def result_text(rec: Dict[str, Any]) -> str:
    """Human/agent-readable summary of a finished job, for the follow-up."""
    out = _read_output(rec)
    if rec.get("killed"):
        head = "Background job was killed."
    elif rec.get("timed_out"):
        head = f"Background job timed out after {rec.get('max_runtime_s')}s."
    elif rec.get("died"):
        head = "Background job process died unexpectedly (no exit code)."
    else:
        head = f"Background job finished with exit code {rec.get('exit_code')}."
    return f"{head}\nCommand: {rec.get('command')}\n\nOutput:\n{out or '(no output)'}"
