"""Restart-safe background job completion and Agent continuation.

A completion is never discarded merely because process-local ownership was
lost. Runtime V3 durable session ordering decides whether the launching run is
still current. Follow-up execution is admitted atomically by ``agent_runs`` and
its assistant continuation is persisted by a terminal callback before the
session becomes idle again.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from src import bg_jobs

logger = logging.getLogger(__name__)

_monitor_task = None
POLL_INTERVAL_S = 5
_FOLLOWUP_MAX_ROUNDS = 12


def _observe_chunk(chunk: str, state: dict[str, Any]) -> None:
    if not chunk.startswith("data: "):
        return
    body = chunk[6:].strip()
    if not body or body == "[DONE]":
        return
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return
    if not isinstance(payload, dict):
        return
    if "delta" in payload:
        delta = payload.get("delta")
        if isinstance(delta, str) and not payload.get("thinking"):
            state["full"] += delta
    elif payload.get("type") == "agent_step":
        state["round"] = payload.get("round", state["round"])
    elif payload.get("type") == "tool_output":
        state["tool_events"].append(
            {
                "round": state["round"],
                "tool": payload.get("tool"),
                "command": payload.get("command"),
                "output": payload.get("output"),
                "exit_code": payload.get("exit_code"),
            }
        )


async def _drain_managed_agent(
    sess,
    messages,
    *,
    terminal_callback: Callable[[dict[str, Any], Any], None],
) -> bool:
    """Start only if idle and persist through a pre-idle terminal callback."""
    from src import agent_runs
    from src.agent.api import stream_agent_loop

    state: dict[str, Any] = {
        "full": "",
        "tool_events": [],
        "round": 1,
        "persisted": False,
    }
    source = stream_agent_loop(
        sess.endpoint_url,
        sess.model,
        messages,
        headers=getattr(sess, "headers", None),
        context_length=getattr(sess, "context_length", 0) or 0,
        session_id=sess.id,
        max_rounds=_FOLLOWUP_MAX_ROUNDS,
        owner=getattr(sess, "owner", None),
        workload="background_followup",
    )

    async def observed_source():
        async for chunk in source:
            _observe_chunk(chunk, state)
            yield chunk

    observed = observed_source()

    def finalize(terminal) -> None:
        terminal_callback(state, terminal)

    run = agent_runs.start_if_idle(
        sess.id,
        observed,
        mode=agent_runs.RunMode.BACKGROUND,
        owner=str(getattr(sess, "owner", None) or "") or None,
        terminal_callback=finalize,
    )
    if run is None:
        await observed.aclose()
        logger.info("bg-followup: session %s became busy; deferring", sess.id)
        return False

    async for _event in agent_runs.subscribe(sess.id):
        pass
    return bool(state["persisted"])


async def _persist_recovery_notice(sm, sess, rec: dict, reason: str) -> bool:
    """Persist a visible, non-executing notice under the same idle admission."""
    from core.models import ChatMessage

    message = (
        f"Background job {rec.get('id')} finished, but Odysseus did not auto-continue it "
        f"because {reason}. The result is preserved and can be resumed explicitly.\n\n"
        f"{bg_jobs.result_text(rec)[:4000]}"
    )

    async def notice_source():
        yield 'data: {"type":"run_state","state":"completed","terminal":true,"reason":"background_notice"}\n\n'
        yield "data: [DONE]\n\n"

    from src import agent_runs

    persisted = {"value": False}

    def finalize(_terminal) -> None:
        sm.add_message(
            sess.id,
            ChatMessage(
                "assistant",
                message,
                metadata={
                    "bg_job_id": rec.get("id"),
                    "bg_result": bg_jobs.result_text(rec)[:4000],
                    "background_recovery_notice": True,
                    "recovery_reason": reason,
                },
            ),
        )
        sm.save_sessions()
        persisted["value"] = True

    source = notice_source()
    run = agent_runs.start_if_idle(
        sess.id,
        source,
        mode=agent_runs.RunMode.BACKGROUND,
        owner=str(getattr(sess, "owner", None) or "") or None,
        terminal_callback=finalize,
    )
    if run is None:
        await source.aclose()
        return False
    async for _event in agent_runs.subscribe(sess.id):
        pass
    return persisted["value"]


def _durable_relation(rec: dict) -> str:
    run_id = str(rec.get("run_id") or "")
    session_id = str(rec.get("session_id") or "")
    if not run_id or not session_id:
        return "unknown"
    from src.agent.runtime_v3.ledger import get_runtime_ledger

    return get_runtime_ledger().session_run_relation(session_id, run_id)


async def _run_followup(rec: dict) -> bool:
    """Continue once, defer while busy, or persist a visible recovery notice."""
    from core.models import ChatMessage
    from src.ai_interaction import get_session_manager

    sm = get_session_manager()
    if not sm:
        return False
    sess = sm.get_session(rec["session_id"])
    if not sess:
        logger.info(
            "bg-followup: session %s gone for job %s; marking handled",
            rec.get("session_id"),
            rec.get("id"),
        )
        return True

    relation = _durable_relation(rec)
    if relation == "superseded":
        return await _persist_recovery_notice(
            sm,
            sess,
            rec,
            "a newer accepted turn superseded the run that launched it",
        )
    if relation == "unknown":
        return await _persist_recovery_notice(
            sm,
            sess,
            rec,
            "durable ownership evidence is unavailable for this legacy job",
        )

    inject = (
        f"[Background job {rec['id']} finished]\n\n"
        f"{bg_jobs.result_text(rec)}\n\n"
        "Continue the task using this output. Do not repeat completed work or "
        "assume any unconfirmed prior effect was rolled back. If the task is "
        "complete, give the user the final result."
    )
    context = sess.get_context_messages()
    context.append({"role": "user", "content": inject})

    def persist(state: dict[str, Any], terminal) -> None:
        disposition = str(getattr(getattr(terminal, "disposition", None), "value", ""))
        if disposition == "cancelled":
            return
        full = str(state["full"] or "").strip()
        if not full:
            full = (
                "The background continuation ended without model prose. "
                f"Terminal reason: {getattr(terminal, 'reason', 'unknown')}."
            )
        sm.add_message(
            sess.id,
            ChatMessage(
                "assistant",
                full,
                metadata={
                    "tool_events": list(state["tool_events"]),
                    "model": sess.model,
                    "bg_job_id": rec["id"],
                    "bg_result": bg_jobs.result_text(rec)[:4000],
                    "background_terminal": disposition,
                    "background_terminal_reason": getattr(terminal, "reason", None),
                },
            ),
        )
        sm.save_sessions()
        state["persisted"] = True
        logger.info(
            "bg-followup: persisted session %s job %s (%d chars, %d tools)",
            sess.id,
            rec["id"],
            len(full),
            len(state["tool_events"]),
        )

    return await _drain_managed_agent(sess, context, terminal_callback=persist)


async def _loop():
    while True:
        try:
            for rec in bg_jobs.pending_followups():
                try:
                    if await _run_followup(rec):
                        bg_jobs.mark_followed_up(rec["id"])
                except Exception as exc:
                    logger.warning(
                        "bg-followup failed for %s (will retry): %s",
                        rec.get("id"),
                        exc,
                        exc_info=True,
                    )
        except Exception as exc:
            logger.warning("bg-monitor tick error: %s", exc, exc_info=True)
        await asyncio.sleep(POLL_INTERVAL_S)


def start_bg_monitor():
    """Idempotently start the always-on background-job monitor."""
    global _monitor_task
    if _monitor_task and not _monitor_task.done():
        return _monitor_task
    _monitor_task = asyncio.create_task(_loop())
    logger.info("Background-job monitor started (poll %ds)", POLL_INTERVAL_S)
    return _monitor_task
