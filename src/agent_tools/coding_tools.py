import json
from typing import Any, Dict, List

from src.agent.planning.service import PLAN_SERVICE


class TodoWriteTool:
    """Legacy wire-compatible adapter over PlanService.

    Root cause: this tool used to persist its own independent JSON file per
    session (src/agent/planning/service.py's module docstring has the full
    history). It now holds no state of its own — every call replaces the
    canonical plan's steps via PLAN_SERVICE.replace(), which is the same
    backend `update_plan`/`manage_plan` use. The input/output shape (args,
    validation errors, output text format) is unchanged so existing callers
    and prompts keep working.
    """

    async def execute(self, content: str, ctx: dict) -> dict:
        try:
            args = json.loads(content) if (content or "").strip().startswith("{") else {"todos": []}
        except (json.JSONDecodeError, TypeError):
            return {"error": "todowrite: JSON object required", "exit_code": 1}
        todos = args.get("todos")
        if not isinstance(todos, list):
            return {"error": "todowrite: todos must be a list", "exit_code": 1}

        normalized: List[Dict[str, Any]] = []
        allowed_statuses = {"pending", "in_progress", "completed"}
        allowed_priorities = {"low", "medium", "high"}
        active_count = 0
        for item in todos:
            if not isinstance(item, dict):
                return {"error": "todowrite: each todo must be an object", "exit_code": 1}
            content_text = str(item.get("content") or item.get("text") or "").strip()
            if not content_text:
                return {"error": "todowrite: todo content required", "exit_code": 1}
            status = str(item.get("status") or "pending").strip()
            if status not in allowed_statuses:
                return {"error": f"todowrite: invalid status {status!r}", "exit_code": 1}
            if status == "in_progress":
                active_count += 1
            priority = str(item.get("priority") or "medium").strip()
            if priority not in allowed_priorities:
                priority = "medium"
            normalized.append({
                "content": content_text,
                "status": status,
                "priority": priority,
            })
        if active_count > 1:
            return {"error": "todowrite: only one todo can be in_progress", "exit_code": 1}

        owner = ctx.get("owner")
        session_id = str(ctx.get("session_id") or args.get("session_id") or "current")
        plan = await PLAN_SERVICE.replace(
            owner_id=owner, session_id=session_id, steps=normalized
        )

        lines = []
        for step in plan.steps:
            marker = {"pending": " ", "in_progress": ">", "completed": "x"}[step.status.value]
            lines.append(f"[{marker}] {step.content} ({step.priority.value})")
        return {
            "output": "Updated todo list:\n" + ("\n".join(lines) if lines else "(empty)"),
            "exit_code": 0,
            "todos": normalized,
            "plan_id": plan.id,
            "plan_version": plan.version,
        }
