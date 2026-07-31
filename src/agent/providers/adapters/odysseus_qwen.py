"""Odysseus-Qwen routing guards and deterministic tool summaries."""

import json
import re
from typing import Any

from src.agent.execution.result_adapters import (
    calendar_list_summary,
    email_list_summary,
    email_read_summary,
    note_list_summary,
    resolved_tool_event_name,
)


def looks_like_notes_turn(text: str) -> bool:
    q = (text or "").lower()
    if re.search(r"\b(notes?|todos?|to-?do|checklists?|reminders?)\b", q):
        return True
    if re.search(
        r"\b(?:take|jot|write down|add|create|make)\b.{0,80}"
        r"\b(?:note|todo|to-?do|checklist|reminder)\b",
        q,
    ):
        return True
    if re.search(r"\b(?:buy|pick ?up|pickup)\b", q) and not re.search(
        r"\b(?:calendar|event|meeting|appointment|schedule)\b",
        q,
    ):
        return True
    return False


def looks_like_notes_calendar_followup(text: str) -> bool:
    q = (text or "").lower()
    return bool(
        re.search(
            r"\b(?:now\s+)?(?:delete|remove|cancel|update|change|move|edit)"
            r"\b.{0,80}"
            r"\b(?:it|that|this|event|appointment|meeting|note|reminder|task)\b",
            q,
        )
        or re.search(r"\b(?:delete|remove|cancel)\s+(?:it|that|this)\b", q)
    )


def looks_like_memory_identity_turn(text: str) -> bool:
    q = re.sub(r"[^a-z0-9\s'?]", " ", (text or "").lower())
    q = re.sub(r"\bhwho\b", "who", q)
    return bool(
        re.search(
            r"\b("
            r"who am i|who i am|what'?s my name|what is my name|"
            r"where do i live|what do you know about me|about me|"
            r"relate to me|use what you know|remember\b|forget\b|"
            r"my preference|my preferences|i prefer|my memory|"
            r"memories about me"
            r")\b",
            q,
        )
    )


def terminal_tool_summary(tool_event: dict[str, Any]) -> str:
    """Return a deterministic user-facing answer for safely rendered tools."""

    tool_name = resolved_tool_event_name(tool_event)
    output = str(tool_event.get("output") or "")
    action = ""
    try:
        args = json.loads(tool_event.get("command") or "{}")
        if isinstance(args, dict):
            action = str(args.get("action") or "").lower()
    except Exception:
        action = ""

    if tool_name == "manage_notes" and action in {
        "list",
        "search",
        "find",
        "view",
        "lis",
    }:
        return note_list_summary(output)
    if tool_name == "manage_calendar" and action in {
        "list",
        "list_events",
        "lis_events",
    }:
        return calendar_list_summary(output)
    if tool_name in {"list_emails", "mcp__email__list_emails"}:
        return email_list_summary(output)
    if tool_name in {"read_email", "mcp__email__read_email"}:
        return email_read_summary(output)
    return ""


DESTRUCTIVE_REQUEST_RE = re.compile(
    r"\b(delete|remove|archive|trash|send|reply|unsubscribe|mark\s+.*read)\b",
    re.IGNORECASE,
)

SUCCESS_CLAIM_RE = re.compile(
    r"\b(done|removed|deleted|sent|archived|unsubscribed|marked)\b",
    re.IGNORECASE,
)


def looks_like_destructive_request(text: str) -> bool:
    return bool(DESTRUCTIVE_REQUEST_RE.search(text or ""))


def looks_like_success_claim(text: str) -> bool:
    return bool(SUCCESS_CLAIM_RE.search(text or ""))
