"""Durable Agent run lifecycle owned by the canonical ``AgentRunner``.

Runtime V3 is a durability service, not a second orchestrator. The service
journals the existing wire stream for replay compatibility, but semantic
terminal state is consumed from typed :class:`src.agent.events.AgentEvent`
objects whenever the canonical Agent event encoder produced the wire event.
Parsing terminal state out of SSE remains only as a compatibility fallback for
legacy producers that bypass the typed event boundary.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict
import json
import time
import uuid
from typing import Any, AsyncGenerator, Callable, Deque

from src.agent.events import AgentEvent, observe_agent_events

from .config import load_runtime_v3_limits
from .contracts import RunStatus
from .ledger import DurableRunLedger, get_runtime_ledger
from .request_identity import redacted_endpoint, safe_request_snapshot


TerminalState = tuple[RunStatus, str, bool]


def event_type_from_wire(wire: str) -> str:
    """Classify replay metadata without treating wire text as lifecycle truth."""

    if not isinstance(wire, str) or not wire.startswith("data: "):
        return "wire"
    try:
        payload = json.loads(wire[6:].strip())
    except Exception:
        return "wire"
    if not isinstance(payload, dict):
        return "wire"
    return str(payload.get("type") or "delta")


def _terminal_from_payload(payload: Any) -> TerminalState | None:
    if not isinstance(payload, dict):
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
        "awaiting_input": RunStatus.INCOMPLETE,
        "awaiting_approval": RunStatus.INCOMPLETE,
        "blocked": RunStatus.INCOMPLETE,
    }.get(state, RunStatus.INCOMPLETE)
    return mapped, str(payload.get("reason") or state), bool(payload.get("resumable"))


def terminal_from_agent_event(event: AgentEvent) -> TerminalState | None:
    """Resolve terminal semantics from the typed pre-serialization event."""

    if event.kind != "run_state":
        return None
    return _terminal_from_payload(dict(event.payload))


def terminal_from_legacy_wire(wire: str) -> TerminalState | None:
    """Compatibility parser for producers that do not emit ``AgentEvent``."""

    if not isinstance(wire, str) or not wire.startswith("data: "):
        return None
    try:
        payload = json.loads(wire[6:].strip())
    except Exception:
        return None
    return _terminal_from_payload(payload)


class DurableRunLifecycle:
    """Journal and terminalize one canonical Agent run.

    A terminal ``AgentEvent`` is observed while it is encoded. The service
    delays the durable transition until the exact encoded string is yielded by
    the backend, preserving the historical ordering where the replay event is
    appended before the run row becomes terminal. String parsing is consulted
    only if no typed terminal corresponds to that yielded wire object.
    """

    def __init__(
        self,
        *,
        ledger: DurableRunLedger | None = None,
        legacy_terminal_parser: Callable[[str], TerminalState | None] = terminal_from_legacy_wire,
    ) -> None:
        self._ledger = ledger
        self._legacy_terminal_parser = legacy_terminal_parser
        self._typed_terminals: Deque[tuple[str, TerminalState]] = deque()

    @property
    def ledger(self) -> DurableRunLedger:
        return self._ledger or get_runtime_ledger()

    def observe_typed_event(self, event: AgentEvent, wire: str) -> None:
        terminal = terminal_from_agent_event(event)
        if terminal is not None:
            self._typed_terminals.append((wire, terminal))

    def _typed_terminal_for_wire(self, wire: str) -> TerminalState | None:
        """Return a terminal only for the exact string object that was encoded."""

        match_index = None
        match = None
        for index, (encoded_wire, terminal) in enumerate(self._typed_terminals):
            if encoded_wire is wire:
                match_index = index
                match = terminal
                break
        if match_index is None:
            return None
        # Any earlier terminal was encoded but never yielded through this
        # backend and therefore must not terminalize the durable run.
        for _ in range(match_index + 1):
            self._typed_terminals.popleft()
        return match

    def terminal_for_wire(self, wire: str) -> TerminalState | None:
        typed = self._typed_terminal_for_wire(wire)
        if typed is not None:
            return typed
        return self._legacy_terminal_parser(wire)

    async def stream(
        self,
        request,
        backend_factory: Callable[[], Any],
    ) -> AsyncGenerator[str, None]:
        ledger = self.ledger
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

        started = time.monotonic()
        terminal_seen = False
        stream = None
        try:
            stream = backend_factory()
            while True:
                remaining = limits.wall_clock_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    ledger.transition(
                        run_id,
                        RunStatus.INCOMPLETE,
                        reason="wall_clock_budget_exhausted",
                        resumable=True,
                    )
                    raise TimeoutError("agent wall-clock budget exhausted")
                try:
                    # Keep the task-local observer installed only while the
                    # backend advances. It is reset before the wire is yielded
                    # to transport/UI code, preventing cross-layer observation.
                    with observe_agent_events(self.observe_typed_event):
                        wire = await asyncio.wait_for(
                            stream.__anext__(),
                            timeout=min(limits.idle_seconds, remaining),
                        )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as exc:
                    ledger.transition(
                        run_id,
                        RunStatus.INCOMPLETE,
                        reason="idle_timeout",
                        resumable=True,
                    )
                    raise TimeoutError("agent stream idle timeout") from exc

                ledger.append_event(
                    run_id,
                    event_type_from_wire(wire),
                    {"wire": wire},
                    max_bytes=limits.max_event_bytes,
                )
                terminal = self.terminal_for_wire(wire)
                if terminal is not None:
                    status, reason, resumable = terminal
                    ledger.transition(
                        run_id,
                        status,
                        reason=reason,
                        resumable=resumable,
                    )
                    terminal_seen = True
                yield wire

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
            current = ledger.get_run(run_id)
            if current and not current.status.terminal:
                ledger.transition(
                    run_id,
                    RunStatus.CANCELLED,
                    reason="task_cancelled",
                    resumable=True,
                )
            raise
        except BaseException as exc:
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
            if stream is not None:
                aclose = getattr(stream, "aclose", None)
                if callable(aclose):
                    try:
                        await aclose()
                    except Exception:
                        pass
            self._typed_terminals.clear()


__all__ = [
    "DurableRunLifecycle",
    "TerminalState",
    "event_type_from_wire",
    "terminal_from_agent_event",
    "terminal_from_legacy_wire",
]
