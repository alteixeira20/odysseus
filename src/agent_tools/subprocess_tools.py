import asyncio
import base64
import hashlib
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import collections
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Callable, Awaitable, Tuple, Dict
from src.constants import MAX_OUTPUT_CHARS
from src.execution_policy import ExecutionMode, normalize_execution_mode
from src.process_sandbox import SandboxUnavailable, sandbox_command
from core.platform_compat import find_bash, git_bash_path, kill_process_tree

DEFAULT_BASH_TIMEOUT = 120
MIN_BASH_TIMEOUT = 1
MAX_BASH_TIMEOUT = 30 * 60
DEFAULT_PYTHON_TIMEOUT = 60 * 60

PROGRESS_INTERVAL_S = 2.0
PROGRESS_TAIL_LINES = 12
TMUX_CAPTURE_LINES = 2000
INTERRUPT_GRACE_S = 0.5
TERMINATE_GRACE_S = 0.5
KILL_GRACE_S = 1.0


@dataclass(frozen=True)
class BashExecutionResult:
    stdout: str
    stderr: str
    exit_code: int
    state: str
    timeout_seconds: float
    invocation_id: str
    escalation: Tuple[str, ...] = ()
    session_recovery: str = "not_applicable"

    @property
    def timed_out(self) -> bool:
        return self.state == "timed_out"


class _BoundedCapture:
    def __init__(self, limit: int = MAX_OUTPUT_CHARS * 2):
        self.limit = max(int(limit), 2)
        self.half = self.limit // 2
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        head_room = max(self.half - len(self.head), 0)
        if head_room:
            self.head.extend(chunk[:head_room])
            chunk = chunk[head_room:]
        if chunk:
            self.tail.extend(chunk)
            if len(self.tail) > self.half:
                del self.tail[:-self.half]

    def text(self) -> str:
        data = bytes(self.head + self.tail)
        omitted = self.total - len(data)
        if omitted > 0:
            data = (
                bytes(self.head)
                + f"\n... ({omitted} bytes omitted) ...\n".encode()
                + bytes(self.tail)
            )
        return data.decode("utf-8", errors="replace")


def normalize_bash_timeout(value) -> float:
    """Return a finite foreground timeout clamped to the public 1-1800s range."""
    if value is None or isinstance(value, bool):
        return float(DEFAULT_BASH_TIMEOUT)
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(DEFAULT_BASH_TIMEOUT)
    if not math.isfinite(parsed):
        return float(DEFAULT_BASH_TIMEOUT)
    return min(max(parsed, float(MIN_BASH_TIMEOUT)), float(MAX_BASH_TIMEOUT))


_TMUX_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)
_FALLBACK_CWDS: Dict[Tuple[str, str, str], str] = {}


def _tmux_lock(name: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    locks = _TMUX_LOCKS.setdefault(loop, {})
    return locks.setdefault(name, asyncio.Lock())


def _tmux_session_name(session_id: Optional[str], workspace: Optional[str] = None) -> str:
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id or "default")).strip("-")
    base = f"ody-agent-{raw[:64] or 'default'}"
    if not workspace:
        return base
    canonical = os.path.realpath(workspace)
    suffix = hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()[:12]
    return f"{base}-{suffix}"


async def _run_exec(*args: str, timeout: float = 10) -> Tuple[str, str, int]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        return "", "timeout", 124
    except asyncio.CancelledError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=1)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        raise
    return (
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
        proc.returncode or 0,
    )


async def _tmux_has_session(name: str) -> bool:
    _, _, rc = await _run_exec("tmux", "has-session", "-t", name, timeout=3)
    return rc == 0


async def _tmux_capture(name: str) -> str:
    out, _, _ = await _run_exec(
        "tmux", "capture-pane", "-p", "-J", "-S", f"-{TMUX_CAPTURE_LINES}", "-t", name,
        timeout=5,
    )
    return out


async def _tmux_send_line(name: str, line: str) -> None:
    if line:
        await _run_exec("tmux", "send-keys", "-t", name, "-l", line, timeout=5)
    await _run_exec("tmux", "send-keys", "-t", name, "C-m", timeout=5)


async def _ensure_tmux_session(name: str, cwd: str, env: Optional[dict]) -> None:
    if await _tmux_has_session(name):
        await _run_exec("tmux", "send-keys", "-t", name, "stty -echo", "C-m", timeout=5)
        return
    await _run_exec(
        "tmux", "new-session", "-d", "-s", name, "-c", cwd,
        "env",
        f"TERM={env.get('TERM', 'xterm-256color') if env else 'xterm-256color'}",
        f"COLUMNS={env.get('COLUMNS', '120') if env else '120'}",
        f"LINES={env.get('LINES', '40') if env else '40'}",
        "/bin/bash",
        "--noprofile",
        "--norc",
        timeout=10,
    )
    if not await _tmux_has_session(name):
        raise RuntimeError(f"failed to create tmux session {name}")
    await _run_exec("tmux", "send-keys", "-t", name, "stty -echo", "C-m", timeout=5)


def _completion_from_capture(capture: str, end_marker: str) -> Optional[int]:
    pattern = re.compile(rf"^{re.escape(end_marker)}:(-?\d+):completed$")
    for line in reversed(capture.splitlines()):
        match = pattern.fullmatch(line.strip())
        if match:
            return int(match.group(1))
    return None


def _read_pid(path: Path, expected_invocation_id: Optional[str] = None) -> Optional[int]:
    try:
        raw = path.read_text(encoding="ascii").strip()
        pid_text, marker = (raw.split(":", 1) + [""])[:2]
        pid = int(pid_text)
    except (OSError, TypeError, ValueError):
        return None
    if pid <= 1:
        return None
    if expected_invocation_id and marker != expected_invocation_id:
        return None
    proc_environ = Path(f"/proc/{pid}/environ")
    if expected_invocation_id and proc_environ.exists():
        try:
            environment = proc_environ.read_bytes().split(b"\0")
        except (OSError, PermissionError):
            return None
        ownership_marker = f"ODY_INVOCATION_ID={expected_invocation_id}".encode("ascii")
        if ownership_marker not in environment:
            return None
    return pid


def _read_bounded(path: Path, limit: int = MAX_OUTPUT_CHARS * 2) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size <= limit:
                data = handle.read()
            else:
                half = max(limit // 2, 1)
                head = handle.read(half)
                handle.seek(max(size - half, 0))
                tail = handle.read(half)
                data = head + (
                    f"\n... ({size - len(head) - len(tail)} bytes omitted) ...\n".encode()
                ) + tail
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _read_tail(path: Path, limit: int = 32_768) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(size - limit, 0))
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _combined_tail(stdout_path: Path, stderr_path: Path) -> str:
    out_lines = _read_tail(stdout_path).splitlines()
    err_lines = [f"! {line}" for line in _read_tail(stderr_path).splitlines()]
    return "\n".join((out_lines + err_lines)[-PROGRESS_TAIL_LINES:])


def _owned_group_alive(pgid: Optional[int]) -> bool:
    if not pgid or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_group_gone(pgid: Optional[int], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _owned_group_alive(pgid):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _terminate_owned_group(pgid: Optional[int]) -> Tuple[str, ...]:
    """Terminate only a command-owned session/process group with bounded escalation."""
    if not pgid or pgid <= 1 or not _owned_group_alive(pgid):
        return ()
    try:
        if os.getpgid(pgid) != pgid:
            return ("ownership_mismatch",)
    except ProcessLookupError:
        return ()

    escalation = []
    for sig, label, grace in (
        (signal.SIGINT, "sigint", INTERRUPT_GRACE_S),
        (signal.SIGTERM, "sigterm", TERMINATE_GRACE_S),
        (signal.SIGKILL, "sigkill", KILL_GRACE_S),
    ):
        try:
            os.killpg(pgid, sig)
            escalation.append(label)
        except ProcessLookupError:
            break
        if await _wait_group_gone(pgid, grace):
            break
    return tuple(escalation)


def _isolated_shell_script(*, capture_to_files: bool = False) -> str:
    capture = (
        'exec > "$ODY_STDOUT_FILE" 2> "$ODY_STDERR_FILE"; '
        if capture_to_files
        else ""
    )
    return (
        capture
        + 'printf "%s:%s\\n" "$$" "$ODY_INVOCATION_ID" > "$ODY_PID_FILE"; '
        "__ody_finish() { "
        "__ody_rc=$?; "
        'printf "%s" "$PWD" > "$ODY_CWD_FILE"; '
        'printf "%s" "$__ody_rc" > "$ODY_RC_FILE"; '
        "}; "
        "trap __ody_finish EXIT; "
        'eval "$(printf "%s" "$ODY_COMMAND_B64" | base64 -d)"'
    )


def _tmux_wrapper(
    content: str,
    *,
    invocation_id: str,
    start_marker: str,
    end_marker: str,
    cwd: str,
    pid_path: Path,
    cwd_path: Path,
    rc_path: Path,
    stdout_path: Path,
    stderr_path: Path,
) -> str:
    command_b64 = base64.b64encode(content.encode("utf-8", errors="surrogatepass")).decode("ascii")
    env_assignments = " ".join(
        f"{key}={shlex.quote(str(value))}"
        for key, value in (
            ("ODY_COMMAND_B64", command_b64),
            ("ODY_INVOCATION_ID", invocation_id),
            ("ODY_PID_FILE", pid_path),
            ("ODY_CWD_FILE", cwd_path),
            ("ODY_RC_FILE", rc_path),
            ("ODY_STDOUT_FILE", stdout_path),
            ("ODY_STDERR_FILE", stderr_path),
        )
    )
    return (
        f"printf '\\n%s\\n' {shlex.quote(start_marker)}; "
        f"{env_assignments} setsid -f -w /bin/bash --noprofile --norc "
        f"-c {shlex.quote(_isolated_shell_script(capture_to_files=True))} "
        "</dev/null >/dev/null 2>/dev/null; "
        "__ody_rc=$?; "
        f"if [ -s {shlex.quote(str(cwd_path))} ]; then "
        f"IFS= read -r __ody_cwd < {shlex.quote(str(cwd_path))} || [ -n \"$__ody_cwd\" ]; "
        f"cd -- \"$__ody_cwd\" 2>/dev/null || cd -- {shlex.quote(cwd)}; "
        "fi; "
        f"printf '\\n%s:%s:completed\\n' {shlex.quote(end_marker)} \"$__ody_rc\"; "
        "unset __ody_rc __ody_cwd ODY_COMMAND_B64 ODY_INVOCATION_ID ODY_PID_FILE ODY_CWD_FILE "
        "ODY_RC_FILE ODY_STDOUT_FILE ODY_STDERR_FILE"
    )


async def _wait_for_tmux_completion(name: str, end_marker: str, timeout: float) -> Optional[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc = _completion_from_capture(await _tmux_capture(name), end_marker)
        if rc is not None:
            return rc
        await asyncio.sleep(0.05)
    return None


async def _recover_tmux_session(name: str, cwd: str, env: Optional[dict]) -> str:
    if await _tmux_has_session(name):
        await _run_exec("tmux", "kill-session", "-t", name, timeout=5)
    await _ensure_tmux_session(name, cwd, env)
    return "recreated"


async def _run_tmux_bash(
    content: str,
    *,
    session_id: str,
    cwd: str,
    env: Optional[dict],
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    invocation_id: Optional[str] = None,
) -> BashExecutionResult:
    name = _tmux_session_name(session_id, cwd)
    timeout = float(timeout)
    nonce = invocation_id if re.fullmatch(r"[A-Za-z0-9_-]{12,128}", invocation_id or "") else secrets.token_urlsafe(24)
    start_marker = f"__ODYSSEUS_CMD_START_{nonce}__"
    end_marker = f"__ODYSSEUS_CMD_END_{nonce}__"

    async with _tmux_lock(name):
        await _ensure_tmux_session(name, cwd, env)
        temp_dir = Path(tempfile.mkdtemp(prefix=f"odysseus-bash-{nonce[:12]}-"))
        pid_path = temp_dir / "pid"
        cwd_path = temp_dir / "cwd"
        rc_path = temp_dir / "rc"
        stdout_path = temp_dir / "stdout"
        stderr_path = temp_dir / "stderr"
        wrapper = _tmux_wrapper(
            content,
            invocation_id=nonce,
            start_marker=start_marker,
            end_marker=end_marker,
            cwd=cwd,
            pid_path=pid_path,
            cwd_path=cwd_path,
            rc_path=rc_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        started = time.monotonic()
        last_progress = started
        session_recovery = "preserved"
        escalation: Tuple[str, ...] = ()
        try:
            await _tmux_send_line(name, wrapper)
            while True:
                capture = await _tmux_capture(name)
                rc = _completion_from_capture(capture, end_marker)
                if rc is not None:
                    return BashExecutionResult(
                        stdout=_read_bounded(stdout_path),
                        stderr=_read_bounded(stderr_path),
                        exit_code=rc,
                        state="completed",
                        timeout_seconds=timeout,
                        invocation_id=nonce,
                        session_recovery=session_recovery,
                    )

                now = time.monotonic()
                if progress_cb and now - last_progress >= PROGRESS_INTERVAL_S:
                    last_progress = now
                    try:
                        await progress_cb({
                            "elapsed_s": round(now - started, 1),
                            "timeout_seconds": timeout,
                            "tail": _combined_tail(stdout_path, stderr_path),
                            "tmux_session": name,
                            "state": "running",
                            "invocation_id": nonce,
                        })
                    except Exception:
                        pass

                if now - started >= timeout:
                    if progress_cb:
                        try:
                            await progress_cb({
                                "elapsed_s": round(now - started, 1),
                                "timeout_seconds": timeout,
                                "tail": _combined_tail(stdout_path, stderr_path),
                                "tmux_session": name,
                                "state": "timing_out",
                                "invocation_id": nonce,
                            })
                        except Exception:
                            pass
                    pgid = _read_pid(pid_path, nonce)
                    escalation = await _terminate_owned_group(pgid)
                    rc = await _wait_for_tmux_completion(name, end_marker, KILL_GRACE_S)
                    if rc is None:
                        session_recovery = await _recover_tmux_session(name, cwd, env)
                    return BashExecutionResult(
                        stdout=_read_bounded(stdout_path),
                        stderr=_read_bounded(stderr_path),
                        exit_code=124,
                        state="timed_out",
                        timeout_seconds=timeout,
                        invocation_id=nonce,
                        escalation=escalation,
                        session_recovery=session_recovery,
                    )
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pgid = _read_pid(pid_path, nonce)
            try:
                await asyncio.shield(_terminate_owned_group(pgid))
                rc = await asyncio.shield(
                    _wait_for_tmux_completion(name, end_marker, KILL_GRACE_S)
                )
                if rc is None:
                    await asyncio.shield(_recover_tmux_session(name, cwd, env))
            finally:
                raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _clean_tmux_command_output(text: str, wrapped_command: str) -> str:
    lines = text.splitlines()
    wrapped_lines = {ln.rstrip() for ln in wrapped_command.splitlines() if ln.strip()}
    cleaned = []
    for line in lines:
        raw = line.rstrip()
        stripped = raw.strip()
        if not stripped:
            cleaned.append(raw)
            continue
        if stripped in wrapped_lines:
            continue
        if stripped.startswith("__ody_rc=") or stripped.startswith("printf "):
            continue
        if re.fullmatch(r"(?:bash|sh)-[\d.]+\$ ?", stripped):
            continue
        if re.fullmatch(r"[\w.@:/~+-]+[#$] ?", stripped):
            continue
        cleaned.append(raw)
    return "\n".join(cleaned).strip()


async def _run_subprocess_streaming(
    proc: asyncio.subprocess.Process,
    *,
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]] = None,
    terminate_process_group: bool = False,
) -> Tuple[str, str, Optional[int], bool, Tuple[str, ...]]:
    started = time.time()
    stdout_full = _BoundedCapture()
    stderr_full = _BoundedCapture()
    tail = collections.deque(maxlen=PROGRESS_TAIL_LINES)

    async def _reader(stream, full_buf, label: str):
        if stream is None:
            return
        while True:
            chunk = await stream.read(65_536)
            if not chunk:
                break
            full_buf.append(chunk)
            decoded = chunk.decode("utf-8", errors="replace")
            for line in decoded.splitlines():
                tail.append(f"! {line}" if label == "err" else line)

    async def _progress_emitter():
        await asyncio.sleep(PROGRESS_INTERVAL_S)
        while True:
            if progress_cb:
                try:
                    await progress_cb({
                        "elapsed_s": round(time.time() - started, 1),
                        "tail": "\n".join(list(tail)),
                    })
                except Exception:
                    pass
            await asyncio.sleep(PROGRESS_INTERVAL_S)

    rd_out = asyncio.create_task(_reader(proc.stdout, stdout_full, "out"))
    rd_err = asyncio.create_task(_reader(proc.stderr, stderr_full, "err"))
    prog_task = asyncio.create_task(_progress_emitter()) if progress_cb else None

    timed_out = False
    escalation: Tuple[str, ...] = ()
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        if progress_cb:
            try:
                await progress_cb({
                    "elapsed_s": round(time.time() - started, 1),
                    "timeout_seconds": timeout,
                    "tail": "\n".join(list(tail)),
                    "state": "timing_out",
                })
            except Exception:
                pass
        if terminate_process_group:
            if os.name == "posix":
                escalation = await _terminate_owned_group(proc.pid)
            else:
                await asyncio.to_thread(kill_process_tree, proc.pid)
                escalation = ("process_tree_killed",)
        elif proc.returncode is None:
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
    except asyncio.CancelledError:
        if terminate_process_group:
            if os.name == "posix":
                await asyncio.shield(_terminate_owned_group(proc.pid))
            else:
                await asyncio.shield(
                    asyncio.to_thread(kill_process_tree, proc.pid)
                )
        elif proc.returncode is None:
            proc.kill()
        try:
            await asyncio.shield(asyncio.wait_for(proc.wait(), timeout=2))
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        for t in (rd_out, rd_err):
            t.cancel()
        if prog_task is not None:
            prog_task.cancel()
        raise
    finally:
        if prog_task is not None and not prog_task.done():
            prog_task.cancel()
            try:
                await prog_task
            except (asyncio.CancelledError, Exception):
                pass
        for t in (rd_out, rd_err):
            try:
                await asyncio.wait_for(t, timeout=1)
            except Exception:
                pass

    return (
        stdout_full.text(),
        stderr_full.text(),
        proc.returncode,
        timed_out,
        escalation,
    )


async def _run_direct_bash(
    content: str,
    *,
    session_id: Optional[str],
    cwd: str,
    env: Optional[dict],
    timeout: float,
    progress_cb: Optional[Callable[[Dict], Awaitable[None]]],
    invocation_id: str,
    execution_mode: ExecutionMode = ExecutionMode.SANDBOXED,
    preserve_logical_cwd: bool = True,
) -> BashExecutionResult:
    canonical = os.path.realpath(cwd)
    state_key = (
        str(session_id or "default"),
        canonical,
        execution_mode.value,
    )
    run_cwd = (
        _FALLBACK_CWDS.get(state_key, canonical)
        if preserve_logical_cwd
        else canonical
    )
    if not os.path.isdir(run_cwd):
        run_cwd = canonical
    if execution_mode is ExecutionMode.SANDBOXED:
        try:
            inside_workspace = os.path.commonpath(
                (os.path.realpath(run_cwd), canonical)
            ) == canonical
        except ValueError:
            inside_workspace = False
        if not inside_workspace:
            run_cwd = canonical

    temp_dir = Path(tempfile.mkdtemp(prefix=f"odysseus-bash-{invocation_id[:12]}-"))
    cwd_path = temp_dir / "cwd"
    rc_path = temp_dir / "rc"
    pid_path = temp_dir / "pid"
    runtime_environment = {
        "ODY_COMMAND_B64": base64.b64encode(
            content.encode("utf-8", errors="surrogatepass")
        ).decode("ascii"),
        "ODY_INVOCATION_ID": invocation_id,
        "ODY_PID_FILE": str(pid_path),
        "ODY_CWD_FILE": str(cwd_path),
        "ODY_RC_FILE": str(rc_path),
    }
    try:
        if execution_mode is ExecutionMode.HOST:
            bash_executable = find_bash()
            if not bash_executable:
                raise SandboxUnavailable(
                    "Full host shell was authorized, but no Bash executable is installed"
                )
            if os.name == "nt":
                for key in ("ODY_PID_FILE", "ODY_CWD_FILE", "ODY_RC_FILE"):
                    runtime_environment[key] = git_bash_path(
                        Path(runtime_environment[key])
                    )
            sandbox_argv = [
                bash_executable,
                "--noprofile",
                "--norc",
                "-c",
                _isolated_shell_script(),
            ]
            child_env = os.environ.copy()
            child_env.update({
                key: str(value) for key, value in runtime_environment.items()
            })
        else:
            sandbox_argv, child_env = sandbox_command(
                [
                    "/bin/bash",
                    "--noprofile",
                    "--norc",
                    "-c",
                    _isolated_shell_script(),
                ],
                cwd=run_cwd,
                writable_paths=(canonical, temp_dir),
                environment=env,
                runtime_environment=runtime_environment,
                timeout_seconds=timeout,
            )
        process_kwargs = {}
        if os.name == "posix":
            process_kwargs["start_new_session"] = True
        elif os.name == "nt":
            process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = await asyncio.create_subprocess_exec(
            *sandbox_argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            cwd=run_cwd,
            **process_kwargs,
        )
        stdout, stderr, rc, timed_out, escalation = await _run_subprocess_streaming(
            proc,
            timeout=timeout,
            progress_cb=progress_cb,
            terminate_process_group=True,
        )
        try:
            final_cwd = cwd_path.read_text(encoding="utf-8")
        except OSError:
            final_cwd = ""
        if preserve_logical_cwd and final_cwd and os.path.isdir(final_cwd):
            if execution_mode is ExecutionMode.HOST:
                _FALLBACK_CWDS[state_key] = final_cwd
            else:
                try:
                    if os.path.commonpath(
                        (os.path.realpath(final_cwd), canonical)
                    ) == canonical:
                        _FALLBACK_CWDS[state_key] = final_cwd
                except ValueError:
                    pass
        return BashExecutionResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=124 if timed_out else (rc if rc is not None else 1),
            state="timed_out" if timed_out else "completed",
            timeout_seconds=timeout,
            invocation_id=invocation_id,
            escalation=escalation,
            session_recovery="logical_cwd_preserved",
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class BashTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd, _truncate
        arguments = dict(ctx.get("arguments") or {})
        if isinstance(content, dict):
            arguments = {**content, **arguments}
            content = str(
                arguments.get("command")
                or arguments.get("cmd")
                or arguments.get("code")
                or ""
            )
        elif isinstance(content, str) and content.lstrip().startswith("{"):
            try:
                parsed = __import__("json").loads(content)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and any(key in parsed for key in ("command", "cmd", "code")):
                arguments = {**parsed, **arguments}
                content = str(
                    arguments.get("command")
                    or arguments.get("cmd")
                    or arguments.get("code")
                    or ""
                )
        else:
            content = str(content or "")

        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")
        session_id = ctx.get("session_id")
        timeout = normalize_bash_timeout(
            arguments.get("timeout_seconds", arguments.get("timeout"))
        )
        requested_invocation_id = str(ctx.get("invocation_id") or "")
        invocation_id = (
            requested_invocation_id
            if re.fullmatch(r"[A-Za-z0-9_-]{12,128}", requested_invocation_id)
            else secrets.token_urlsafe(24)
        )
        cwd = str(ctx.get("workspace") or agent_cwd())
        execution_mode = normalize_execution_mode(ctx.get("execution_mode"))

        try:
            outcome = await _run_direct_bash(
                content,
                session_id=str(session_id) if session_id else None,
                cwd=cwd,
                env=_subproc_env,
                timeout=timeout,
                progress_cb=progress_cb,
                invocation_id=invocation_id,
                execution_mode=execution_mode,
            )
        except SandboxUnavailable as exc:
            return {
                "error": str(exc),
                "error_type": "sandbox_unavailable",
                "exit_code": 126,
                "completion_state": "blocked",
            }
        tmux_session = None

        _raw_stdout_len = len(outcome.stdout or "")
        _raw_stderr_len = len(outcome.stderr or "")
        stdout = _truncate(outcome.stdout, MAX_OUTPUT_CHARS)
        stderr = _truncate(outcome.stderr, MAX_OUTPUT_CHARS)
        common = {
            "exit_code": outcome.exit_code,
            "stdout": stdout,
            "stderr": stderr,
            # Structured truncation metadata (src/agent/execution/result_budget.py
            # field convention) alongside the human-readable notice _truncate
            # already appends to stdout/stderr — a caller can check these
            # instead of pattern-matching the notice text.
            "stdout_truncated": _raw_stdout_len > MAX_OUTPUT_CHARS,
            "stderr_truncated": _raw_stderr_len > MAX_OUTPUT_CHARS,
            "stdout_total_chars": _raw_stdout_len,
            "stderr_total_chars": _raw_stderr_len,
            "completion_state": outcome.state,
            "timed_out": outcome.timed_out,
            "timeout_seconds": outcome.timeout_seconds,
            "invocation_id": outcome.invocation_id,
            "escalation": list(outcome.escalation),
            "shell_recovery": outcome.session_recovery,
            "execution_mode": execution_mode.value,
        }
        if tmux_session:
            common["tmux_session"] = tmux_session
        if outcome.timed_out:
            common.update({
                "error": f"bash: timed out after {outcome.timeout_seconds:g}s",
                "error_type": "command_timeout",
                "partial_stdout": stdout,
                "partial_stderr": stderr,
            })
            return common

        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        common["output"] = _truncate(output, MAX_OUTPUT_CHARS) or "(no output)"
        return common


class PythonTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import agent_cwd, _truncate
        progress_cb = ctx.get("progress_cb")
        _subproc_env = ctx.get("subproc_env")
        cwd = str(ctx.get("workspace") or agent_cwd())
        execution_mode = normalize_execution_mode(ctx.get("execution_mode"))
        try:
            python_executable = os.path.realpath(sys.executable or "python")
            if execution_mode is ExecutionMode.HOST:
                sandbox_argv = [python_executable, "-I", "-c", content]
                child_env = os.environ.copy()
            else:
                sandbox_argv, child_env = sandbox_command(
                    [python_executable, "-I", "-c", content],
                    cwd=cwd,
                    environment=_subproc_env,
                    timeout_seconds=DEFAULT_PYTHON_TIMEOUT,
                )
        except SandboxUnavailable as exc:
            return {
                "error": str(exc),
                "error_type": "sandbox_unavailable",
                "exit_code": 126,
                "completion_state": "blocked",
            }
        process_kwargs = {}
        if os.name == "posix":
            process_kwargs["start_new_session"] = True
        elif os.name == "nt":
            process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        proc = await asyncio.create_subprocess_exec(
            *sandbox_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
            cwd=cwd,
            **process_kwargs,
        )
        stdout, stderr, rc, timed_out, _escalation = await _run_subprocess_streaming(
            proc,
            timeout=DEFAULT_PYTHON_TIMEOUT,
            progress_cb=progress_cb,
            terminate_process_group=True,
        )
        if timed_out:
            return {"error": f"python: timed out after {DEFAULT_PYTHON_TIMEOUT}s — process killed", "exit_code": 124, "stdout": _truncate(stdout, MAX_OUTPUT_CHARS), "stderr": _truncate(stderr, MAX_OUTPUT_CHARS), "execution_mode": execution_mode.value}
        output = stdout.rstrip()
        err = stderr.rstrip()
        if err:
            output = (output + "\nSTDERR: " + err).strip() if output else "STDERR: " + err
        output = _truncate(output, MAX_OUTPUT_CHARS)
        return {"output": output or "(no output)", "exit_code": rc or 0, "execution_mode": execution_mode.value}
