import json
import logging

from src.agent.planning.service import (
    PLAN_SERVICE,
    markdown_checklist_to_steps,
    steps_to_markdown_checklist,
)

logger = logging.getLogger(__name__)

class AskUserTool:
    async def execute(self, content, ctx):
        """
        ask_user: the agent poses a multiple-choice question to the user to get a
        decision/clarification. This is a pure UI-control marker — no subprocess,
        no filesystem. It returns an `ask_user` payload that the agent loop turns
        into an `ask_user` SSE event and then ENDS the turn, so the chat waits for
        the user's selection (their choice arrives as the next message).
        """
        question, options, multi = "", [], False
        raw = (content or "").strip()
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict):
            question = str(parsed.get("question", "")).strip()
            multi = bool(parsed.get("multi") or parsed.get("multiSelect"))
            for opt in (parsed.get("options") or []):
                if isinstance(opt, dict):
                    label = str(opt.get("label", "")).strip()
                    descr = str(opt.get("description", "")).strip()
                elif isinstance(opt, str):
                    label, descr = opt.strip(), ""
                else:
                    continue
                if label:
                    options.append({"label": label, "description": descr})
        else:
            question = raw

        if not question or len(options) < 2:
            return "ask_user: invalid", {
                "error": (
                    "ask_user needs a non-empty `question` and at least 2 `options` "
                    "(each an object with a `label`, optional `description`)."
                ),
                "exit_code": 1,
            }

        options = options[:6]  # keep the choice list sane
        desc = f"ask_user: {question[:80]}"
        labels = ", ".join(o["label"] for o in options)
        result = {
            "ask_user": {"question": question, "options": options, "multi": multi},
            "output": f"Asked the user: {question}\nOptions: {labels}\nAwaiting their selection.",
            "exit_code": 0,
        }
        logger.info("Tool executed: %s (%d options, multi=%s)", desc, len(options), multi)
        return desc, result

class UpdatePlanTool:
    async def execute(self, content, ctx):
        """
        update_plan: the agent writes back to the active plan — tick an item done
        or revise steps (e.g. when the user asks to change something). Legacy
        wire-compatible adapter over PlanService (src/agent/planning/service.py):
        the markdown checklist is parsed into structured steps and persisted
        through PLAN_SERVICE.replace() — the same canonical, versioned backend
        `todowrite`/`manage_plan` use — then regenerated from those steps for
        the `plan_update` SSE payload, so the frontend's markdown-based plan
        panel keeps working unchanged. Does NOT end the turn.
        """
        raw = (content or "").strip()
        plan = ""
        try:
            parsed = json.loads(raw) if raw else {}
        except (ValueError, TypeError):
            parsed = {}

        if isinstance(parsed, dict) and parsed.get("plan"):
            plan = str(parsed.get("plan", "")).strip()
        else:
            plan = raw

        if not plan:
            return "update_plan: invalid", {
                "error": "update_plan needs a non-empty `plan` (the full updated checklist as markdown).",
                "exit_code": 1,
            }

        plan = plan[:8192]
        steps = markdown_checklist_to_steps(plan)
        owner = ctx.get("owner")
        session_id = str(ctx.get("session_id") or "current")
        stored = await PLAN_SERVICE.replace(
            owner_id=owner, session_id=session_id, steps=steps
        )
        # Regenerate from the canonical steps (not the raw input) so the
        # SSE payload always reflects exactly what was persisted.
        canonical_plan_md = steps_to_markdown_checklist(stored.steps)

        total = len(stored.steps)
        done = sum(1 for s in stored.steps if s.status.value == "completed")
        desc = f"update_plan: {done}/{total} done" if total else "update_plan"
        result = {
            "plan_update": {"plan": canonical_plan_md},
            "output": f"Plan updated ({done}/{total} steps complete)." if total else "Plan updated.",
            "exit_code": 0,
            "plan_id": stored.id,
            "plan_version": stored.version,
        }
        logger.info("Tool executed: %s", desc)
        return desc, result