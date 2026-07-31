"""manage_plan: the canonical, structured planning tool.

Unlike the legacy `todowrite`/`update_plan` adapters (which round-trip
through a flat todo list or a full markdown checklist respectively),
`manage_plan` exposes PlanService's full partial-update surface directly:
add/update/complete/block/skip/remove a single step, or reorder, without
resending the whole plan. All three tools share one backend
(src/agent/planning/service.py) — there is exactly one canonical plan per
owner+session.
"""

import json
import logging

from src.agent.planning.service import (
    PLAN_SERVICE,
    Plan,
    PlanNotFound,
    PlanStepNotFound,
    PlanVersionConflict,
)

logger = logging.getLogger(__name__)

_ACTIONS = (
    "create",
    "read",
    "replace",
    "add_step",
    "update_step",
    "complete_step",
    "block_step",
    "skip_step",
    "remove_step",
    "reorder",
    "clear",
    "archive",
)


def _plan_summary(plan: Plan) -> dict:
    total = len(plan.steps)
    done = sum(1 for s in plan.steps if s.status.value == "completed")
    return {
        "id": plan.id,
        "version": plan.version,
        "title": plan.title,
        "summary": plan.summary,
        "archived": plan.archived_at is not None,
        "steps": [
            {
                "id": s.id,
                "content": s.content,
                "status": s.status.value,
                "priority": s.priority.value,
                "depends_on": list(s.depends_on),
                "notes": s.notes,
            }
            for s in plan.steps
        ],
        "progress": f"{done}/{total}" if total else "0/0",
    }


class ManagePlanTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        raw = (content or "").strip()
        try:
            args = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            return {
                "error": "manage_plan: arguments must be a JSON object",
                "exit_code": 1,
            }
        if not isinstance(args, dict):
            return {
                "error": "manage_plan: arguments must be a JSON object",
                "exit_code": 1,
            }

        action = str(args.get("action") or "").strip()
        if action not in _ACTIONS:
            return {
                "error": (
                    f"manage_plan: unknown action {action!r}. "
                    f"Expected one of: {', '.join(_ACTIONS)}."
                ),
                "exit_code": 1,
            }

        owner = ctx.get("owner")
        session_id = str(ctx.get("session_id") or args.get("session_id") or "current")
        expected_version = args.get("expected_version")

        try:
            if action == "read":
                plan = await PLAN_SERVICE.read(owner_id=owner, session_id=session_id)
                if plan is None:
                    return "manage_plan: read (no plan)", {
                        "output": "No plan exists yet for this session.",
                        "exit_code": 0,
                        "plan": None,
                    }
            elif action == "create":
                plan = await PLAN_SERVICE.create(
                    owner_id=owner,
                    session_id=session_id,
                    title=str(args.get("title") or ""),
                    summary=str(args.get("summary") or ""),
                    steps=args.get("steps") or (),
                )
            elif action == "replace":
                plan = await PLAN_SERVICE.replace(
                    owner_id=owner,
                    session_id=session_id,
                    steps=args.get("steps") or (),
                    title=args.get("title"),
                    summary=args.get("summary"),
                    expected_version=expected_version,
                )
            elif action == "add_step":
                content_text = str(args.get("content") or "").strip()
                if not content_text:
                    return {
                        "error": "manage_plan add_step needs non-empty `content`.",
                        "exit_code": 1,
                    }
                plan = await PLAN_SERVICE.add_step(
                    owner_id=owner,
                    session_id=session_id,
                    content=content_text,
                    priority=str(args.get("priority") or "medium"),
                    acceptance_criteria=str(args.get("acceptance_criteria") or ""),
                    depends_on=args.get("depends_on") or (),
                    expected_version=expected_version,
                )
            elif action == "update_step":
                step_id = str(args.get("step_id") or "")
                if not step_id:
                    return {
                        "error": "manage_plan update_step needs `step_id`.",
                        "exit_code": 1,
                    }
                plan = await PLAN_SERVICE.update_step(
                    owner_id=owner,
                    session_id=session_id,
                    step_id=step_id,
                    content=args.get("content"),
                    status=args.get("status"),
                    priority=args.get("priority"),
                    acceptance_criteria=args.get("acceptance_criteria"),
                    notes=args.get("notes"),
                    depends_on=args.get("depends_on"),
                    expected_version=expected_version,
                )
            elif action == "complete_step":
                step_id = str(args.get("step_id") or "")
                if not step_id:
                    return {
                        "error": "manage_plan complete_step needs `step_id`.",
                        "exit_code": 1,
                    }
                plan = await PLAN_SERVICE.complete_step(
                    owner_id=owner,
                    session_id=session_id,
                    step_id=step_id,
                    evidence=args.get("evidence"),
                    expected_version=expected_version,
                )
            elif action == "block_step":
                step_id = str(args.get("step_id") or "")
                if not step_id:
                    return {
                        "error": "manage_plan block_step needs `step_id`.",
                        "exit_code": 1,
                    }
                plan = await PLAN_SERVICE.block_step(
                    owner_id=owner,
                    session_id=session_id,
                    step_id=step_id,
                    notes=str(args.get("notes") or ""),
                    expected_version=expected_version,
                )
            elif action == "skip_step":
                step_id = str(args.get("step_id") or "")
                if not step_id:
                    return {
                        "error": "manage_plan skip_step needs `step_id`.",
                        "exit_code": 1,
                    }
                plan = await PLAN_SERVICE.skip_step(
                    owner_id=owner,
                    session_id=session_id,
                    step_id=step_id,
                    notes=str(args.get("notes") or ""),
                    expected_version=expected_version,
                )
            elif action == "remove_step":
                step_id = str(args.get("step_id") or "")
                if not step_id:
                    return {
                        "error": "manage_plan remove_step needs `step_id`.",
                        "exit_code": 1,
                    }
                plan = await PLAN_SERVICE.remove_step(
                    owner_id=owner,
                    session_id=session_id,
                    step_id=step_id,
                    expected_version=expected_version,
                )
            elif action == "reorder":
                step_ids = args.get("step_ids") or []
                if not isinstance(step_ids, list) or not step_ids:
                    return {
                        "error": "manage_plan reorder needs a non-empty `step_ids` list.",
                        "exit_code": 1,
                    }
                plan = await PLAN_SERVICE.reorder(
                    owner_id=owner,
                    session_id=session_id,
                    step_ids=step_ids,
                    expected_version=expected_version,
                )
            elif action == "clear":
                plan = await PLAN_SERVICE.clear(
                    owner_id=owner,
                    session_id=session_id,
                    expected_version=expected_version,
                )
            elif action == "archive":
                plan = await PLAN_SERVICE.archive(
                    owner_id=owner,
                    session_id=session_id,
                    expected_version=expected_version,
                )
        except PlanVersionConflict as exc:
            return {
                "error": (
                    f"manage_plan: version conflict — you had "
                    f"{exc.expected_version}, latest is {exc.latest.version}. "
                    "Re-read the plan and retry with the current version."
                ),
                "error_type": "plan_version_conflict",
                "conflict": True,
                "latest_version": exc.latest.version,
                "latest_plan": _plan_summary(exc.latest),
                "exit_code": 1,
            }
        except PlanNotFound as exc:
            return {
                "error": f"manage_plan: {exc}",
                "exit_code": 1,
            }
        except PlanStepNotFound as exc:
            return {
                "error": f"manage_plan: no such step {exc.step_id!r}.",
                "exit_code": 1,
            }
        except ValueError as exc:
            # Invalid enum value (bad status/priority string).
            return {"error": f"manage_plan: {exc}", "exit_code": 1}

        summary = _plan_summary(plan)
        desc = f"manage_plan: {action} ({summary['progress']} done)"
        result = {
            "output": f"Plan {action} ok — {summary['progress']} steps complete.",
            "exit_code": 0,
            "plan": summary,
            "plan_update": {"plan_id": plan.id, "version": plan.version, "action": action},
        }
        logger.info("Tool executed: %s", desc)
        return desc, result
