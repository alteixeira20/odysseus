from __future__ import annotations

import asyncio
import json
from typing import Any, Literal, Mapping

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.agent.runtime_v3.contracts import RunStatus
from src.agent.runtime_v3.operations import (
    RuntimeAccessDenied,
    RuntimeConflict,
    get_runtime_operations,
)
from src.auth_helpers import effective_user


class CancelRunRequest(BaseModel):
    reason: str = Field(default="user_requested", max_length=1000)


class ReconcileEffectRequest(BaseModel):
    outcome: Literal["committed", "failed"]
    note: str = Field(min_length=8, max_length=4000)
    expected_revision: int = Field(ge=0)
    evidence: Mapping[str, Any] = Field(default_factory=dict)


def _owner(request: Request) -> str | None:
    return effective_user(request)


def _translate_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RuntimeAccessDenied):
        return HTTPException(status_code=404, detail="Run not found")
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail="Run or effect not found")
    if isinstance(exc, RuntimeConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="Runtime operation failed")


def setup_agent_runtime_routes() -> APIRouter:
    router = APIRouter(prefix="/api/agent-runs", tags=["agent-runtime"])

    @router.get("")
    async def list_agent_runs(
        request: Request,
        session_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        before: float | None = Query(default=None),
    ) -> dict[str, Any]:
        try:
            runs = get_runtime_operations().list_runs(
                owner=_owner(request),
                session_id=session_id,
                status=status,
                limit=limit,
                before=before,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc
        return {"runs": runs, "count": len(runs)}

    @router.get("/{run_id}")
    async def get_agent_run(request: Request, run_id: str) -> dict[str, Any]:
        try:
            return get_runtime_operations().get_run(run_id, owner=_owner(request))
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.get("/{run_id}/events")
    async def get_agent_run_events(
        request: Request,
        run_id: str,
        after_seq: int = Query(default=0, ge=0),
        limit: int = Query(default=8192, ge=1, le=100000),
    ) -> dict[str, Any]:
        try:
            operations = get_runtime_operations()
            window = operations.event_window(run_id, owner=_owner(request))
            events = operations.events(
                run_id,
                owner=_owner(request),
                after_seq=after_seq,
                limit=limit,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc
        return {
            "run_id": run_id,
            "events": events,
            "next_seq": events[-1]["seq"] if events else max(
                after_seq,
                window["available_from_seq"] - 1,
            ),
            "available_from_seq": window["available_from_seq"],
            "last_event_seq": window["last_event_seq"],
            "retained_bytes": window["retained_bytes"],
            "truncated": after_seq < window["available_from_seq"] - 1,
        }

    @router.get("/{run_id}/stream")
    async def stream_agent_run_events(
        request: Request,
        run_id: str,
        after_seq: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        operations = get_runtime_operations()
        owner = _owner(request)
        try:
            operations.get_run(run_id, owner=owner)
        except Exception as exc:
            raise _translate_error(exc) from exc

        async def generate():
            window = operations.event_window(run_id, owner=owner)
            cursor = after_seq
            if cursor < window["available_from_seq"] - 1:
                yield "event: replay_gap\n"
                yield "data: " + json.dumps(
                    {
                        "requested_after_seq": cursor,
                        "available_from_seq": window["available_from_seq"],
                        "last_event_seq": window["last_event_seq"],
                    },
                    separators=(",", ":"),
                ) + "\n\n"
                cursor = window["available_from_seq"] - 1
            heartbeat = 0
            while True:
                if await request.is_disconnected():
                    return
                events = operations.events(
                    run_id,
                    owner=owner,
                    after_seq=cursor,
                    limit=1024,
                )
                for event in events:
                    cursor = max(cursor, int(event["seq"]))
                    yield "id: " + str(cursor) + "\n"
                    yield "event: runtime_event\n"
                    yield "data: " + json.dumps(event, separators=(",", ":"), default=str) + "\n\n"
                run = operations.get_run(run_id, owner=owner)
                if RunStatus(run["status"]).terminal and not events:
                    yield "event: terminal\n"
                    yield "data: " + json.dumps(
                        {
                            "run_id": run_id,
                            "status": run["status"],
                            "reason": run["terminal_reason"],
                            "last_event_seq": run["last_event_seq"],
                        },
                        separators=(",", ":"),
                    ) + "\n\n"
                    return
                if not events:
                    heartbeat += 1
                    yield f": heartbeat {heartbeat}\n\n"
                await asyncio.sleep(0.75)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/{run_id}/cancel")
    async def cancel_agent_run(
        request: Request,
        run_id: str,
        body: CancelRunRequest,
    ) -> dict[str, Any]:
        try:
            return get_runtime_operations().request_cancel(
                run_id,
                owner=_owner(request),
                reason=body.reason,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    @router.post("/{run_id}/effects/{effect_id}/reconcile")
    async def reconcile_agent_effect(
        request: Request,
        run_id: str,
        effect_id: str,
        body: ReconcileEffectRequest,
    ) -> dict[str, Any]:
        try:
            return get_runtime_operations().reconcile_effect(
                run_id,
                effect_id,
                owner=_owner(request),
                outcome=body.outcome,
                note=body.note,
                expected_revision=body.expected_revision,
                evidence=body.evidence,
            )
        except Exception as exc:
            raise _translate_error(exc) from exc

    return router
