from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import time
import uuid
from typing import Any, AsyncGenerator, Callable

from .config import load_runtime_v3_limits
from .contracts import RunStatus
from .ledger import get_runtime_ledger
from .request_identity import redacted_endpoint, safe_request_snapshot
from .stream_journal import DurableStreamJournal


def _event_type(wire: str) -> str:
    if not isinstance(wire, str) or not wire.startswith("data: "):
        return "wire"
    try:
        payload = json.loads(wire[6:].strip())
    except Exception:
        return "wire"
    return str(payload.get("type") or "delta")


def _terminal_from_wire(wire: str) -> tuple[RunStatus, str, bool] | None:
    if not isinstance(wire, str) or not wire.startswith("data: "):
        return None
    try:
        payload = json.loads(wire[6:].strip())
    except Exception:
        return None
    if payload.get("type") != "run_state" or not payload.get("terminal"):
        return None
    state = str(payload.get("state") or payload.get("disposition") or "incomplete")
    mapped = {
        "completed": RunStatus.COMPLETED,
        "cancelled": RunStatus.CANCELLED,
        "error": RunStatus.FAILED,
        "failed": RunStatus.FAILED,
        "incomplete": RunStatus.INCOMPLETE,
        "budget_exhausted": RunStatus.INCOMPLETE,
        "rounds_exhausted": RunStatus.INCOMPLETE,
    }.get(state, RunStatus.INCOMPLETE)
    return mapped, str(payload.get("reason") or state), bool(payload.get("resumable"))


async def stream_with_durable_runtime(request, legacy_factory: Callable[[], Any]) -> AsyncGenerator[str, None]:
    """Wrap the compatibility runtime with durable run/event lifecycle state.

    High-frequency transient SSE signals are coalesced only in the durable
    ledger. The live wire contract is unchanged, while lifecycle/effect/error
    events remain immediate durable boundaries. Durable request identity binds
    the complete semantic request via a SHA-256 digest without copying prompt or
    credential plaintext into the runtime database.
    """
    ledger = get_runtime_ledger()
    limits = load_runtime_v3_limits().normalize_request(
        max_rounds=request.limits.max_rounds,
        max_tool_calls=request.limits.max_tool_calls,
    )
    run_id = getattr(request.execution_context, "run_id", None) or f"run-{uuid.uuid4()}"
    ledger.create_run(
        run_id=run_id,
        session_id=request.session_id,
        owner=request.owner,
        workload=request.workload,
        request=safe_request_snapshot(request),
        limits=asdict(limits),
        model=request.model,
        endpoint=redacted_endpoint(request.endpoint_url),
    )
    ledger.transition(run_id, RunStatus.RUNNING, reason="started")
    ledger.append_event(
        run_id,
        "run_started",
        {"run_id": run_id, "limits": asdict(limits)},
        max_bytes=limits.max_event_bytes,
    )
    journal = DurableStreamJournal(
        ledger,
        run_id,
        max_event_bytes=limits.max_event_bytes,
        max_batch_events=limits.stream_batch_events,
        max_batch_bytes=limits.stream_batch_bytes,
    )

    started = time.monotonic()
    stream = legacy_factory()
    terminal_seen = False
    try:
        while True:
            remaining = limits.wall_clock_seconds - (time.monotonic() - started)
            if remaining <= 0:
                journal.flush()
                ledger.transition(
                    run_id,
                    RunStatus.INCOMPLETE,
                    reason="wall_clock_budget_exhausted",
                    resumable=True,
                )
                raise TimeoutError("agent wall-clock budget exhausted")
            try:
                event = await asyncio.wait_for(
                    stream.__anext__(),
                    timeout=min(limits.idle_seconds, remaining),
                )
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                journal.flush()
                ledger.transition(run_id, RunStatus.INCOMPLETE, reason="idle_timeout", resumable=True)
                raise TimeoutError("agent stream idle timeout") from exc

            journal.record(_event_type(event), event)
            terminal = _terminal_from_wire(event)
            if terminal:
                status, reason, resumable = terminal
                ledger.transition(run_id, status, reason=reason, resumable=resumable)
                terminal_seen = True
            yield event

        journal.flush()
        if not terminal_seen:
            ledger.transition(
                run_id,
                RunStatus.INCOMPLETE,
                reason="stream_ended_without_terminal",
                resumable=True,
            )
            ledger.append_event(
                run_id,
                "runtime_warning",
                {"reason": "stream_ended_without_terminal"},
                max_bytes=limits.max_event_bytes,
            )
    except asyncio.CancelledError:
        try:
            journal.flush()
        except Exception:
            # Cancellation must not be replaced by a secondary journal error.
            pass
        current = ledger.get_run(run_id)
        if current and not current.status.terminal:
            ledger.transition(run_id, RunStatus.CANCELLED, reason="task_cancelled", resumable=True)
        raise
    except BaseException as exc:
        try:
            journal.flush()
        except Exception:
            # Preserve the original runtime failure; critical events were never
            # buffered, and unresolved effects remain governed by the ledger.
            pass
        current = ledger.get_run(run_id)
        if current and not current.status.terminal:
            ledger.transition(
                run_id,
                RunStatus.FAILED,
                reason=type(exc).__name__,
                resumable=True,
                error={"type": type(exc).__name__, "message": str(exc)[:2000]},
            )
        raise
    finally:
        aclose = getattr(stream, "aclose", None)
        if callable(aclose):
            try:
                await aclose()
            except Exception:
                pass
