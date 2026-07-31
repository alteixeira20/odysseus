"""Detect a model announcing agentic work without making the tool call."""

import re
from typing import Optional

from src.agent.contracts import SupervisorAction, SupervisorDecision


INTENT_WITHOUT_ACTION_RE = re.compile(
    r"(?:^|\n)\s*(?:let me|i'?ll|i will|i need to|we need to|need to|"
    r"i should|we should|i must|we must|going to|let's)\s+"
    r"(?:tail|check|investigate|look at|see|tail|read|fetch|inspect|"
    r"verify|diagnose|examine|debug|capture|grab|pull|view|run|call|"
    r"trigger|launch|start|kick off|stop|kill|restart|adopt|serve|"
    r"register|adopt|list|search|find|query|hit|ping|test|use|perform|do)"
    r"\b[^.\n]{0,140}",
    re.IGNORECASE,
)


def evaluate_intent_without_action(
    text: str,
    *,
    nudge_count: int,
    max_nudges: int = 2,
    guide_only: bool = False,
) -> Optional[SupervisorDecision]:
    intent_text = str(text or "").strip()
    match = (
        INTENT_WITHOUT_ACTION_RE.search(intent_text)
        if intent_text
        else None
    )
    looks_like_promise = (
        not guide_only
        and match is not None
        and len(intent_text) < 400
        and "```" not in intent_text
    )
    if not looks_like_promise:
        return None

    matched = match.group(0).strip()
    if nudge_count >= max_nudges:
        return SupervisorDecision(
            action=SupervisorAction.FINISH,
            reason="intent_without_action_nudge_cap",
            metadata={
                "matched": matched,
                "nudges": nudge_count,
            },
        )

    cookbook_hint = ""
    lowered = matched.lower()
    if any(
        word in lowered
        for word in ("log", "logs", "output", "tail", "status")
    ):
        cookbook_hint = (
            " If this is about a Cookbook/model serve, the concrete calls are: "
            "`list_served_models` first, then `tail_serve_output` with the "
            "session_id from the serve/list result. Never answer with "
            '"check logs" when those tools are available.'
        )
    instruction = (
        f'You just wrote: "{matched}" — but ended the '
        "turn without making the actual tool call. The user can "
        "see you announced the action but didn't run it, which "
        "is the most frustrating thing you can do. "
        "DO IT NOW: emit the actual function call this turn. "
        f"{cookbook_hint}"
        "If you decided not to do it after all, say so plainly in "
        "one sentence instead of restating the plan."
    )
    return SupervisorDecision(
        action=SupervisorAction.RETRY_WITH_INSTRUCTION,
        reason="intent_without_action",
        instruction=instruction,
        metadata={
            "matched": matched,
            "nudges": nudge_count + 1,
        },
    )
