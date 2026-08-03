"""In-memory run ownership, replay, and terminal-state persistence.

Subscriber presence is delivery state, never execution ownership.  Normal
chat runs are DETACHED: closing the SSE removes only that subscriber; explicit
stop/replacement/server shutdown owns cancellation.  ATTACHED is available for
single-client streams whose disconnect intentionally owns cancellation, while
BACKGROUND is subscriber-independent and retained with the same in-process
durability boundary as the rest of this manager.

Durability scope remains the server process.  Terminal output is retained for a
bounded reconnect grace period; it does not survive a process restart.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import logging
import math
import os
import re
import stat
import sys
import tempfile
import threading
import time
from typing import AsyncGenerator, Dict, Optional

from src.agent.contracts import RunDisposition
from src.agent.runtime_v2.contracts import AgentExecutionContext
from src.agent.runtime_v2.events import (
    encode_runtime_sse,
    runtime_event_from_payload,
)
from src.agent.runtime_v2.state import RunState, RunStateMachine
from src.agent.runtime_v2.ownership import OwnershipError, RUN_OWNERSHIP, TurnLease


logger = logging.getLogger(__name__)


class RunMode(str, Enum):
    ATTACHED = "attached"
    DETACHED = "detached"
    BACKGROUND = "background"


@dataclass(frozen=True)
class RunTerminal:
    disposition: RunDisposition
    reason: str
    payload: dict


class RunCapacityError(RuntimeError):
    """Raised before start when the server/owner detached-run quota is full."""


@dataclass(frozen=True)
class PreparedTurn:
    lease: TurnLease
    expected_run: Optional["_Run"]


class _RunLimitExceeded(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Run:
    __slots__ = (
        "base_seq",
        "buffer",
        "buffer_bytes",
        "evict_task",
        "mode",
        "owner",
        "last_event_at",
        "started_at",
        "started_monotonic",
        "status",
        "subscribers",
        "task",
        "terminal",
        "terminal_callback",
        "execution_context",
        "state_machine",
    )

    def __init__(
        self,
        mode: RunMode,
        owner: Optional[str],
        execution_context: Optional[AgentExecutionContext],
        terminal_callback=None,
    ) -> None:
        self.base_seq = 0
        self.buffer: list[str] = []
        self.buffer_bytes = 0
        self.subscribers: set[asyncio.Queue] = set()
        self.status = "running"
        self.task: Optional[asyncio.Task] = None
        self.evict_task: Optional[asyncio.Task] = None
        self.started_at = time.time()
        self.started_monotonic = time.monotonic()
        self.last_event_at = self.started_monotonic
        self.mode = mode
        self.owner = owner
        self.terminal: Optional[RunTerminal] = None
        self.terminal_callback = terminal_callback
        self.execution_context = execution_context
        self.state_machine = RunStateMachine(
            execution_context.run_id if execution_context else f"legacy:{id(self)}"
        )


_RUNS: Dict[str, _Run] = {}
_RUNS_LOCK = threading.RLock()
_RUNTIME_STATE_LOCK_FD: Optional[int] = None
_RUNTIME_STATE_LOCK_PID: Optional[int] = None
_RUNTIME_STATE_LOCK_KEY: Optional[str] = None

_EVICT_GRACE_S = 180
_MAX_REPLAY_EVENTS = 4_096
_MAX_REPLAY_BYTES = 4 * 1024 * 1024
_MAX_SUBSCRIBER_QUEUE = 256


def _positive_env_number(name: str, default: float, cast):
    try:
        value = cast(os.getenv(name, str(default)))
    except (TypeError, ValueError, OverflowError):
        value = cast(default)
    if isinstance(value, float) and not math.isfinite(value):
        value = cast(default)
    return max(cast(1), value)


_MAX_ACTIVE_RUNS = _positive_env_number(
    "ODYSSEUS_AGENT_MAX_ACTIVE_RUNS", 8, int
)
_MAX_ACTIVE_RUNS_PER_OWNER = _positive_env_number(
    "ODYSSEUS_AGENT_MAX_ACTIVE_RUNS_PER_OWNER", 3, int
)
_MAX_RUN_WALL_CLOCK_S = _positive_env_number(
    "ODYSSEUS_AGENT_MAX_RUN_SECONDS", 900.0, float
)
_MAX_RUN_IDLE_S = _positive_env_number(
    "ODYSSEUS_AGENT_RUN_IDLE_SECONDS", 180.0, float
)


def _acquire_runtime_state_lock() -> None:
    """Hold a process lock keyed to the configured Runtime V2 state root."""

    global _RUNTIME_STATE_LOCK_FD, _RUNTIME_STATE_LOCK_PID, _RUNTIME_STATE_LOCK_KEY
    from src.constants import DATA_DIR

    state_root = os.path.realpath(
        os.getenv("ODYSSEUS_RUNTIME_STATE_DIR") or DATA_DIR
    )
    key = hashlib.sha256(state_root.encode("utf-8")).hexdigest()
    pid = os.getpid()
    if (
        _RUNTIME_STATE_LOCK_FD is not None
        and _RUNTIME_STATE_LOCK_PID == pid
        and _RUNTIME_STATE_LOCK_KEY == key
    ):
        return
    if _RUNTIME_STATE_LOCK_FD is not None:
        if _RUNTIME_STATE_LOCK_PID == pid:
            raise RuntimeError(
                "Runtime V2 state directory cannot change after its process lock is acquired"
            )
        try:
            os.close(_RUNTIME_STATE_LOCK_FD)
        except OSError:
            pass
        _RUNTIME_STATE_LOCK_FD = None
    owner_suffix = str(os.getuid()) if hasattr(os, "getuid") else "current-user"
    lock_root = os.path.join(
        tempfile.gettempdir(),
        f"odysseus-runtime-v2-state-locks-{owner_suffix}",
    )
    os.makedirs(lock_root, mode=0o700, exist_ok=True)
    root_info = os.lstat(lock_root)
    if not stat.S_ISDIR(root_info.st_mode):
        raise RuntimeError("Runtime V2 process-lock root is not a directory")
    if hasattr(os, "getuid") and root_info.st_uid != os.getuid():
        raise RuntimeError("Runtime V2 process-lock root has a foreign owner")
    try:
        os.chmod(lock_root, 0o700)
    except OSError:
        pass
    lock_path = os.path.join(lock_root, f"{key}.lock")
    open_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, open_flags, 0o600)
    try:
        lock_info = os.fstat(descriptor)
        if not stat.S_ISREG(lock_info.st_mode):
            raise RuntimeError("Runtime V2 process lock is not a regular file")
        if hasattr(os, "getuid") and lock_info.st_uid != os.getuid():
            raise RuntimeError("Runtime V2 process lock has a foreign owner")
        if os.name != "posix":
            os.ftruncate(descriptor, 1)
            os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "posix":
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        pid_bytes = str(pid).encode("ascii")
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, pid_bytes)
        os.ftruncate(descriptor, max(len(pid_bytes), 1))
        os.fsync(descriptor)
    except BaseException as exc:
        os.close(descriptor)
        if not isinstance(exc, OSError):
            raise
        raise RuntimeError(
            "another Odysseus process already owns this Runtime V2 state directory"
        ) from exc
    _RUNTIME_STATE_LOCK_FD = descriptor
    _RUNTIME_STATE_LOCK_PID = pid
    _RUNTIME_STATE_LOCK_KEY = key


def enforce_single_runtime_worker() -> None:
    """Fail startup when process-local run durability would be split."""

    configured: list[int] = []
    for name in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        raw = os.getenv(name)
        if raw:
            try:
                configured.append(int(raw))
            except ValueError:
                raise RuntimeError(f"{name} must be an integer") from None
    gunicorn = os.getenv("GUNICORN_CMD_ARGS", "")
    match = re.search(r"(?:--workers|-w)\s+(\d+)", gunicorn)
    if match:
        configured.append(int(match.group(1)))
    argv = " ".join(str(item) for item in sys.argv[1:])
    for expression in (
        r"(?:^|\s)--workers(?:=|\s+)(\d+)(?:\s|$)",
        r"(?:^|\s)-w\s+(\d+)(?:\s|$)",
    ):
        match = re.search(expression, argv)
        if match:
            configured.append(int(match.group(1)))
    if any(count != 1 for count in configured):
        raise RuntimeError(
            "Odysseus agent runs and approvals are process-local; configure exactly one runtime worker"
        )
    _acquire_runtime_state_lock()


def _publish(run: _Run, event: str) -> None:
    """Append a bounded replay event and fan it out to live subscribers."""

    run.last_event_at = time.monotonic()
    run.buffer.append(event)
    run.buffer_bytes += len(event.encode("utf-8", errors="replace"))
    while run.buffer and (
        len(run.buffer) > _MAX_REPLAY_EVENTS
        or run.buffer_bytes > _MAX_REPLAY_BYTES
    ):
        removed = run.buffer.pop(0)
        run.buffer_bytes -= len(removed.encode("utf-8", errors="replace"))
        run.base_seq += 1

    seq = run.base_seq + len(run.buffer) - 1
    for queue in list(run.subscribers):
        try:
            queue.put_nowait((seq, event))
        except asyncio.QueueFull:
            # A slow/dead client must not apply unbounded backpressure to a
            # detached producer.  Disconnect its delivery queue; it can resume
            # from the bounded replay buffer.
            run.subscribers.discard(queue)
            try:
                while True:
                    queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait((None, None))
            except asyncio.QueueFull:
                pass


def _schedule_evict(session_id: str) -> None:
    run = _RUNS.get(session_id)
    if run is None:
        return
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()

    async def _evict(run_ref: _Run) -> None:
        try:
            await asyncio.sleep(_EVICT_GRACE_S)
        except asyncio.CancelledError:
            return
        current = _RUNS.get(session_id)
        if current is run_ref and current.status != "running" and not current.subscribers:
            _RUNS.pop(session_id, None)

    run.evict_task = asyncio.create_task(_evict(run))


def is_active(session_id: str) -> bool:
    run = _RUNS.get(session_id)
    return bool(run and run.status == "running")


def get_status(session_id: str) -> Optional[str]:
    run = _RUNS.get(session_id)
    return run.status if run else None


def list_runs(owner: Optional[str] = None) -> list[dict]:
    """Return bounded operational metadata, never buffered message content."""

    now = time.time()
    records = []
    for session_id, run in _RUNS.items():
        if owner is not None and run.owner != owner:
            continue
        records.append({
            "session_id": session_id,
            "owner": run.owner,
            "mode": run.mode.value,
            "status": run.status,
            "age_seconds": round(max(0.0, now - run.started_at), 3),
            "subscribers": len(run.subscribers),
            "replay_events": len(run.buffer),
            "replay_bytes": run.buffer_bytes,
            "terminal": (
                run.terminal.disposition.value if run.terminal else None
            ),
            "run_id": (
                run.execution_context.run_id
                if run.execution_context
                else None
            ),
            "run_state": run.state_machine.state.value,
            "execution_mode": (
                run.execution_context.execution_mode.value
                if run.execution_context
                else None
            ),
            "root_source": (
                run.execution_context.execution_root.source.value
                if run.execution_context
                else None
            ),
            "workspace_revision": (
                run.execution_context.execution_root.workspace_revision
                if run.execution_context
                else None
            ),
            "authority_revision": (
                run.execution_context.authority_grant.revision
                if run.execution_context
                else None
            ),
        })
    return sorted(records, key=lambda record: record["age_seconds"], reverse=True)


def _parse_terminal_event(event: str) -> Optional[RunTerminal]:
    if not event.startswith("data: {"):
        return None
    try:
        payload = json.loads(event[6:])
    except (TypeError, ValueError):
        return None
    if payload.get("type") != "run_state" or not payload.get("terminal"):
        return None
    try:
        disposition = RunDisposition(str(payload.get("state")))
    except ValueError:
        disposition = RunDisposition.INCOMPLETE
        payload = {
            **payload,
            "state": disposition.value,
            "reason": "unknown_terminal_state",
            "resumable": True,
        }
    return RunTerminal(
        disposition=disposition,
        reason=str(payload.get("reason") or disposition.value),
        payload=dict(payload),
    )


def _parse_runtime_wire(event: str):
    if not event.startswith("data: {"):
        return None
    try:
        payload = json.loads(event[6:])
    except (TypeError, ValueError):
        return None
    return runtime_event_from_payload(payload)


def _runtime_state_for_disposition(disposition: RunDisposition) -> RunState:
    if disposition is RunDisposition.COMPLETED:
        return RunState.COMPLETED
    if disposition is RunDisposition.CANCELLED:
        return RunState.CANCELLED
    if disposition is RunDisposition.ERROR:
        return RunState.FAILED
    if disposition is RunDisposition.AWAITING_INPUT:
        return RunState.WAITING_USER
    if disposition is RunDisposition.AWAITING_APPROVAL:
        return RunState.WAITING_APPROVAL
    return RunState.INCOMPLETE


def _terminal_from_runtime(runtime_event) -> Optional[RunTerminal]:
    if runtime_event is None or runtime_event.type != "run_state":
        return None
    try:
        state = RunState(str(runtime_event.payload.get("state")))
    except ValueError:
        return None
    if not state.terminal:
        return None
    raw_disposition = runtime_event.payload.get("disposition")
    if raw_disposition:
        try:
            disposition = RunDisposition(str(raw_disposition))
        except ValueError:
            disposition = RunDisposition.INCOMPLETE
    else:
        disposition = {
            RunState.COMPLETED: RunDisposition.COMPLETED,
            RunState.CANCELLED: RunDisposition.CANCELLED,
            RunState.FAILED: RunDisposition.ERROR,
            RunState.WAITING_USER: RunDisposition.AWAITING_INPUT,
            RunState.WAITING_APPROVAL: RunDisposition.AWAITING_APPROVAL,
        }.get(state, RunDisposition.INCOMPLETE)
    return RunTerminal(
        disposition=disposition,
        reason=str(runtime_event.payload.get("reason") or disposition.value),
        payload={
            "type": "run_state",
            "state": disposition.value,
            "terminal": True,
            "reason": str(runtime_event.payload.get("reason") or disposition.value),
            "resumable": bool(runtime_event.payload.get("resumable")),
        },
    )


def _prefer_terminal(
    pending: Optional[RunTerminal], candidate: RunTerminal
) -> RunTerminal:
    """Preserve the first decision unless a later one corrects false success."""

    if pending is None:
        return candidate
    if (
        pending.disposition is RunDisposition.COMPLETED
        and candidate.disposition is not RunDisposition.COMPLETED
    ):
        return candidate
    return pending


def _terminal_event(terminal: RunTerminal) -> str:
    return f"data: {json.dumps(terminal.payload)}\n\n"


def _terminal_status(disposition: RunDisposition) -> str:
    if disposition is RunDisposition.COMPLETED:
        return "done"
    if disposition is RunDisposition.CANCELLED:
        return "stopped"
    return disposition.value


def _make_terminal(
    disposition: RunDisposition,
    *,
    reason: str,
    resumable: bool = False,
) -> RunTerminal:
    return RunTerminal(
        disposition=disposition,
        reason=reason,
        payload={
            "type": "run_state",
            "state": disposition.value,
            "terminal": True,
            "reason": reason,
            "resumable": resumable,
        },
    )


def _commit_terminal(
    run: _Run,
    terminal: RunTerminal,
    *,
    runtime_wire: Optional[str] = None,
) -> None:
    callback = run.terminal_callback
    run.terminal_callback = None
    if callback is not None:
        try:
            callback(terminal)
        except Exception:
            logger.exception("run terminal callback failed")
            terminal = _make_terminal(
                RunDisposition.ERROR,
                reason="terminal_callback_failed",
                resumable=True,
            )
    run.terminal = terminal
    run.status = _terminal_status(terminal.disposition)
    runtime_state = _runtime_state_for_disposition(terminal.disposition)
    if runtime_wire is not None:
        parsed = _parse_runtime_wire(runtime_wire)
        if parsed is not None:
            try:
                run.state_machine.reduce(parsed)
            except ValueError:
                logger.exception("invalid terminal runtime event for %s", parsed.run_id)
                runtime_wire = None
    if runtime_wire is None and run.execution_context is not None:
        event = run.execution_context.event_factory.create(
            "run_state",
            {
                "state": runtime_state.value,
                "disposition": terminal.disposition.value,
                "reason": terminal.reason,
                "resumable": bool(terminal.payload.get("resumable")),
                "terminal": True,
            },
        )
        runtime_wire = encode_runtime_sse(event)
        try:
            run.state_machine.reduce(event)
        except ValueError:
            if runtime_state is RunState.CANCELLED:
                run.state_machine.transition(
                    runtime_state,
                    reason=terminal.reason,
                    sequence=event.sequence,
                    cancellation_override=True,
                )
            else:
                logger.exception("failed to commit runtime terminal state")
    if runtime_wire is not None:
        _publish(run, runtime_wire)
    _publish(run, _terminal_event(terminal))
    _publish(run, "data: [DONE]\n\n")
    if run.execution_context is not None:
        RUN_OWNERSHIP.end_run(run.execution_context)


async def _drain(
    session_id: str,
    agen: AsyncGenerator[str, None],
    prev_task: Optional[asyncio.Task] = None,
) -> None:
    """Drain a producer independently from its current subscriber count."""

    run = _RUNS.get(session_id)
    if run is None:
        return
    if prev_task is not None and not prev_task.done():
        try:
            await asyncio.wait({prev_task}, timeout=1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    pending_terminal: Optional[RunTerminal] = None
    pending_runtime_wire: Optional[str] = None
    stream_error_seen = False
    protocol_done_seen = False
    try:
        while True:
            wall_limit = (
                run.execution_context.budgets.wall_clock_seconds
                if run.execution_context
                else _MAX_RUN_WALL_CLOCK_S
            )
            idle_limit = (
                run.execution_context.budgets.idle_seconds
                if run.execution_context
                else _MAX_RUN_IDLE_S
            )
            wall_remaining = (
                wall_limit
                - (time.monotonic() - run.started_monotonic)
            )
            if wall_remaining <= 0:
                raise _RunLimitExceeded("run_wall_clock_exhausted")
            next_timeout = min(idle_limit, wall_remaining)
            try:
                event = await asyncio.wait_for(
                    agen.__anext__(),
                    timeout=next_timeout,
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                reason = (
                    "run_wall_clock_exhausted"
                    if wall_remaining <= idle_limit
                    else "run_idle_timeout"
                )
                raise _RunLimitExceeded(reason)
            if protocol_done_seen:
                logger.warning(
                    "[agent-run] ignored event emitted after [DONE] for %s",
                    session_id,
                )
                continue
            if run.execution_context is not None:
                try:
                    RUN_OWNERSHIP.validate_context(run.execution_context)
                except OwnershipError:
                    # A newer user turn owns this conversation.  Do not commit
                    # late text/events from the old producer; tool dispatch has
                    # the same independent ownership check.
                    try:
                        await agen.aclose()
                    except Exception:
                        pass
                    run.status = "stopped"
                    break
            if event.startswith("event: error"):
                stream_error_seen = True
            runtime_event = _parse_runtime_wire(event)
            if runtime_event is not None and run.execution_context is not None:
                try:
                    RUN_OWNERSHIP.validate_event(
                        run.execution_context,
                        runtime_event,
                    )
                except OwnershipError:
                    logger.warning(
                        "[agent-run] rejected stale runtime event for %s",
                        session_id,
                    )
                    continue
            runtime_terminal = _terminal_from_runtime(runtime_event)
            if runtime_terminal is not None:
                chosen_terminal = _prefer_terminal(
                    pending_terminal, runtime_terminal
                )
                if chosen_terminal is runtime_terminal:
                    pending_runtime_wire = event
                pending_terminal = chosen_terminal
                continue
            if runtime_event is not None and runtime_event.type == "run_state":
                try:
                    run.state_machine.reduce(runtime_event)
                except ValueError:
                    logger.exception(
                        "invalid runtime state event run=%s sequence=%s",
                        runtime_event.run_id,
                        runtime_event.sequence,
                    )
                    continue
                _publish(run, event)
                continue
            candidate = _parse_terminal_event(event)
            if candidate is not None:
                # Do not publish/commit terminal metadata until [DONE] confirms
                # normal protocol completion.  Cancellation/exception can still
                # override this uncommitted decision.
                chosen_terminal = _prefer_terminal(pending_terminal, candidate)
                if chosen_terminal is candidate:
                    pending_runtime_wire = None
                pending_terminal = chosen_terminal
                continue
            if event == "data: [DONE]\n\n":
                protocol_done_seen = True
                if stream_error_seen and (
                    pending_terminal is None
                    or pending_terminal.disposition is RunDisposition.COMPLETED
                ):
                    pending_terminal = _make_terminal(
                        RunDisposition.ERROR,
                        reason="stream_error",
                    )
                    pending_runtime_wire = None
                if pending_terminal is None:
                    pending_terminal = _make_terminal(
                        RunDisposition.INCOMPLETE,
                        reason="done_without_semantic_terminal",
                        resumable=True,
                    )
                _commit_terminal(
                    run,
                    pending_terminal,
                    runtime_wire=pending_runtime_wire,
                )
                # Resume the wrapped generator so its post-yield cleanup/finally
                # runs normally.  Any further wire events are protocol noise and
                # are ignored above.
                continue
            _publish(run, event)

        if not protocol_done_seen and run.status == "running":
            # An exhausted generator is not proof that the provider/runtime
            # completed its task or its wire protocol.
            terminal = _make_terminal(
                RunDisposition.INCOMPLETE,
                reason="generator_closed_without_done",
                resumable=True,
            )
            _commit_terminal(run, terminal)
    except _RunLimitExceeded as exc:
        if not protocol_done_seen:
            try:
                await agen.aclose()
            except Exception:
                pass
            _commit_terminal(
                run,
                _make_terminal(
                    RunDisposition.INCOMPLETE,
                    reason=exc.reason,
                    resumable=True,
                ),
            )
    except asyncio.CancelledError:
        if protocol_done_seen:
            return
        run.status = "stopped"
        try:
            from src import bg_jobs

            killed = bg_jobs.kill_for_session_since(session_id, run.started_at)
            if killed:
                logger.info(
                    "[agent-run] cancelled %s background job(s) for %s",
                    killed,
                    session_id,
                )
        except Exception:
            logger.exception(
                "[agent-run] failed to cancel background jobs for %s", session_id
            )
        try:
            await agen.aclose()
        except Exception:
            pass
        _commit_terminal(
            run,
            _make_terminal(RunDisposition.CANCELLED, reason="run_cancelled"),
        )
    except Exception as exc:
        if protocol_done_seen:
            logger.warning(
                "[agent-run] producer raised after protocol completion for %s: %s",
                session_id,
                exc,
                exc_info=True,
            )
            return
        logger.error("[agent-run] %s failed: %s", session_id, exc, exc_info=True)
        _publish(
            run,
            "event: error\n"
            + f"data: {json.dumps({'error': 'Agent run failed before completion.', 'status': 500})}\n\n",
        )
        _commit_terminal(
            run,
            _make_terminal(RunDisposition.ERROR, reason="run_exception"),
        )
    finally:
        for queue in list(run.subscribers):
            try:
                queue.put_nowait((None, None))
            except asyncio.QueueFull:
                pass
        if run.status != "running":
            _schedule_evict(session_id)


def start(
    session_id: str,
    agen: AsyncGenerator[str, None],
    *,
    mode: RunMode = RunMode.DETACHED,
    owner: Optional[str] = None,
    execution_context: Optional[AgentExecutionContext] = None,
    prepared_turn: Optional[PreparedTurn] = None,
    commit_callback=None,
    terminal_callback=None,
) -> _Run:
    """Start a run with explicit ownership semantics.

    Replacement remains an explicit owner action at this layer and may cancel
    an in-flight run; subscriber count never does for DETACHED/BACKGROUND.
    """

    if not isinstance(mode, RunMode):
        mode = RunMode(str(mode))
    with _RUNS_LOCK:
        previous = _RUNS.get(session_id)
        active_other_runs = [
            candidate
            for candidate_session, candidate in _RUNS.items()
            if candidate_session != session_id and candidate.status == "running"
        ]
        if len(active_other_runs) >= _MAX_ACTIVE_RUNS:
            raise RunCapacityError("server agent-run concurrency limit reached")
        if owner is not None and sum(
            candidate.owner == owner for candidate in active_other_runs
        ) >= _MAX_ACTIVE_RUNS_PER_OWNER:
            raise RunCapacityError("owner agent-run concurrency limit reached")
        if prepared_turn is not None:
            if previous is not prepared_turn.expected_run:
                raise OwnershipError(
                    "conversation run changed while the replacement request was preparing"
                )
            try:
                if execution_context is not None:
                    from src.agent.runtime_v2.authority import activate_execution_context

                    activate_execution_context(execution_context, prepared_turn.lease)
                else:
                    RUN_OWNERSHIP.commit_prepared_turn(prepared_turn.lease)
                if commit_callback is not None:
                    commit_callback()
            except BaseException:
                RUN_OWNERSHIP.rollback_prepared_turn(prepared_turn.lease)
                raise
            RUN_OWNERSHIP.finalize_prepared_turn(prepared_turn.lease)
        elif execution_context is not None:
            RUN_OWNERSHIP.validate_context(execution_context)
        if prepared_turn is None and commit_callback is not None:
            commit_callback()
        previous_task: Optional[asyncio.Task] = None
        if previous and previous.task and not previous.task.done():
            previous_task = previous.task
        run = _Run(mode, owner, execution_context, terminal_callback)
        _RUNS[session_id] = run
        run.task = asyncio.create_task(_drain(session_id, agen, previous_task))
        if previous:
            if previous_task is not None:
                if previous.execution_context is not None:
                    previous.execution_context.cancellation_token.cancel()
                previous_task.cancel()
            if previous.evict_task and not previous.evict_task.done():
                previous.evict_task.cancel()

    def _ensure_terminal(task: asyncio.Task) -> None:
        # Cancellation can win the race before _drain executes its first line,
        # in which case a try/except inside _drain never runs. Commit the same
        # semantic terminal here so immediate Stop never strands subscribers.
        if run.status != "running":
            return
        if task.cancelled():
            _commit_terminal(
                run,
                _make_terminal(
                    RunDisposition.CANCELLED,
                    reason="run_cancelled",
                ),
            )
            _schedule_evict(session_id)
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "[agent-run] drain task escaped before terminal commit for %s",
                session_id,
                exc_info=(type(error), error, error.__traceback__),
            )
            _commit_terminal(
                run,
                _make_terminal(
                    RunDisposition.ERROR,
                    reason="run_task_exception",
                ),
            )
            _schedule_evict(session_id)

    run.task.add_done_callback(_ensure_terminal)
    return run


def start_if_idle(
    session_id: str,
    agen: AsyncGenerator[str, None],
    *,
    mode: RunMode = RunMode.BACKGROUND,
    owner: Optional[str] = None,
    execution_context: Optional[AgentExecutionContext] = None,
    commit_callback=None,
    terminal_callback=None,
) -> Optional[_Run]:
    """Atomically start background work only while the session is idle.

    Unlike a separate ``is_active`` check followed by ``start``, this cannot
    replace a foreground run that began between those operations.
    """
    with _RUNS_LOCK:
        current = _RUNS.get(str(session_id))
        if current is not None and current.status == "running":
            return None
        return start(
            str(session_id),
            agen,
            mode=mode,
            owner=owner,
            execution_context=execution_context,
            commit_callback=commit_callback,
            terminal_callback=terminal_callback,
        )


def begin_turn(*, session_id: str, owner: Optional[str]) -> TurnLease:
    """Supersede unfinished work as soon as a newer user message is accepted."""

    previous = _RUNS.get(str(session_id))
    if previous and previous.task and not previous.task.done():
        if previous.execution_context is not None:
            previous.execution_context.cancellation_token.cancel()
        previous.task.cancel()
    return RUN_OWNERSHIP.claim_turn(
        owner_id=str(owner or ""),
        conversation_id=str(session_id),
    )


def prepare_turn(*, session_id: str, owner: Optional[str]) -> PreparedTurn:
    """Prepare a replacement without cancelling or superseding valid work."""

    with _RUNS_LOCK:
        previous = _RUNS.get(str(session_id))
        lease = RUN_OWNERSHIP.prepare_turn(
            owner_id=str(owner or ""),
            conversation_id=str(session_id),
        )
        return PreparedTurn(
            lease=lease,
            expected_run=previous,
        )


def commit_prepared_turn(
    prepared: PreparedTurn,
    *,
    session_id: str,
    execution_context: Optional[AgentExecutionContext],
    commit_callback=None,
) -> None:
    """Commit ownership for a direct (non-managed) stream after preparation."""

    with _RUNS_LOCK:
        previous = _RUNS.get(str(session_id))
        if previous is not prepared.expected_run:
            raise OwnershipError(
                "conversation run changed while the replacement request was preparing"
            )
        try:
            if execution_context is not None:
                from src.agent.runtime_v2.authority import activate_execution_context

                activate_execution_context(execution_context, prepared.lease)
            else:
                RUN_OWNERSHIP.commit_prepared_turn(prepared.lease)
            if commit_callback is not None:
                commit_callback()
        except BaseException:
            RUN_OWNERSHIP.rollback_prepared_turn(prepared.lease)
            raise
        RUN_OWNERSHIP.finalize_prepared_turn(prepared.lease)
        if previous and previous.task and not previous.task.done():
            if previous.execution_context is not None:
                previous.execution_context.cancellation_token.cancel()
            previous.task.cancel()


async def subscribe(session_id: str) -> AsyncGenerator[str, None]:
    """Replay the bounded buffer, then stream live events until termination."""

    run = _RUNS.get(session_id)
    if run is None:
        return
    queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_SUBSCRIBER_QUEUE)
    run.subscribers.add(queue)
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()
    try:
        next_seq = run.base_seq
        while next_seq < run.base_seq + len(run.buffer):
            index = next_seq - run.base_seq
            if index < 0:
                next_seq = run.base_seq
                continue
            yield run.buffer[index]
            next_seq += 1
        if run.status != "running":
            return
        heartbeat_index = 0
        while True:
            try:
                seq, event = await asyncio.wait_for(queue.get(), timeout=10.0)
            except asyncio.TimeoutError:
                if run.status == "running":
                    heartbeat_index += 1
                    yield f": heartbeat {heartbeat_index}\n\n"
                    continue
                seq, event = (None, None)
            if seq is None:
                if next_seq < run.base_seq:
                    next_seq = run.base_seq
                while next_seq < run.base_seq + len(run.buffer):
                    yield run.buffer[next_seq - run.base_seq]
                    next_seq += 1
                break
            if seq >= next_seq:
                yield event
                next_seq = seq + 1
    finally:
        run.subscribers.discard(queue)
        if (
            run.mode is RunMode.ATTACHED
            and not run.subscribers
            and run.status == "running"
            and run.task
            and not run.task.done()
        ):
            logger.info(
                "[agent-run] cancelling attached run %s after disconnect", session_id
            )
            if run.execution_context is not None:
                run.execution_context.cancellation_token.cancel()
            run.task.cancel()
        if not run.subscribers and run.status != "running":
            _schedule_evict(session_id)


def stop(session_id: str) -> bool:
    """Explicitly cancel an in-flight run."""

    run = _RUNS.get(session_id)
    if run and run.task and not run.task.done():
        if run.execution_context is not None:
            run.execution_context.cancellation_token.cancel()
        run.task.cancel()
        return True
    return False


__all__ = [
    "RunMode",
    "PreparedTurn",
    "RunCapacityError",
    "RunTerminal",
    "begin_turn",
    "prepare_turn",
    "commit_prepared_turn",
    "get_status",
    "enforce_single_runtime_worker",
    "is_active",
    "list_runs",
    "start",
    "start_if_idle",
    "stop",
    "subscribe",
]
