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
import json
import logging
import math
import os
import time
from typing import AsyncGenerator, Dict, Optional

from src.agent.contracts import RunDisposition


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
    )

    def __init__(self, mode: RunMode, owner: Optional[str]) -> None:
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


_RUNS: Dict[str, _Run] = {}

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


def _commit_terminal(run: _Run, terminal: RunTerminal) -> None:
    run.terminal = terminal
    run.status = _terminal_status(terminal.disposition)
    _publish(run, _terminal_event(terminal))
    _publish(run, "data: [DONE]\n\n")


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
            await asyncio.wait({prev_task})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    pending_terminal: Optional[RunTerminal] = None
    stream_error_seen = False
    protocol_done_seen = False
    try:
        while True:
            wall_remaining = (
                _MAX_RUN_WALL_CLOCK_S
                - (time.monotonic() - run.started_monotonic)
            )
            if wall_remaining <= 0:
                raise _RunLimitExceeded("run_wall_clock_exhausted")
            next_timeout = min(_MAX_RUN_IDLE_S, wall_remaining)
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
                    if wall_remaining <= _MAX_RUN_IDLE_S
                    else "run_idle_timeout"
                )
                raise _RunLimitExceeded(reason)
            if protocol_done_seen:
                logger.warning(
                    "[agent-run] ignored event emitted after [DONE] for %s",
                    session_id,
                )
                continue
            if event.startswith("event: error"):
                stream_error_seen = True
            candidate = _parse_terminal_event(event)
            if candidate is not None:
                # Do not publish/commit terminal metadata until [DONE] confirms
                # normal protocol completion.  Cancellation/exception can still
                # override this uncommitted decision.
                pending_terminal = _prefer_terminal(pending_terminal, candidate)
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
                if pending_terminal is None:
                    pending_terminal = _make_terminal(
                        RunDisposition.COMPLETED,
                        reason="stream_complete",
                    )
                _commit_terminal(run, pending_terminal)
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
) -> _Run:
    """Start a run with explicit ownership semantics.

    Replacement remains an explicit owner action at this layer and may cancel
    an in-flight run; subscriber count never does for DETACHED/BACKGROUND.
    """

    if not isinstance(mode, RunMode):
        mode = RunMode(str(mode))
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
    previous_task: Optional[asyncio.Task] = None
    if previous:
        if previous.task and not previous.task.done():
            previous.task.cancel()
            previous_task = previous.task
        if previous.evict_task and not previous.evict_task.done():
            previous.evict_task.cancel()
    run = _Run(mode, owner)
    _RUNS[session_id] = run
    run.task = asyncio.create_task(_drain(session_id, agen, previous_task))

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
            run.task.cancel()
        if not run.subscribers and run.status != "running":
            _schedule_evict(session_id)


def stop(session_id: str) -> bool:
    """Explicitly cancel an in-flight run."""

    run = _RUNS.get(session_id)
    if run and run.task and not run.task.done():
        run.task.cancel()
        return True
    return False


__all__ = [
    "RunMode",
    "RunCapacityError",
    "RunTerminal",
    "get_status",
    "is_active",
    "list_runs",
    "start",
    "stop",
    "subscribe",
]
