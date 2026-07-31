"""Pure end-of-run response finalization."""

from dataclasses import dataclass
import json

from src.agent.execution.result_adapters import (
    calendar_list_summary,
    email_list_summary,
    email_read_summary,
    note_list_summary,
    resolved_tool_event_name,
)


@dataclass(frozen=True)
class DeterministicToolSummary:
    matched: bool
    text: str = ""


def select_deterministic_tool_summary(
    tool_events: list[dict],
) -> DeterministicToolSummary:
    """Select the legacy final summary for the latest supported tool result."""

    for event in reversed(tool_events):
        tool_name = resolved_tool_event_name(event)
        action = ""
        try:
            arguments = json.loads(event.get("command") or "{}")
            if isinstance(arguments, dict):
                action = str(arguments.get("action") or "").lower()
        except Exception:
            action = ""
        output = event.get("output") or ""
        if (
            tool_name == "manage_notes"
            and action in {"list", "search", "find", "view", "lis"}
        ):
            return DeterministicToolSummary(True, note_list_summary(output))
        if (
            tool_name == "manage_calendar"
            and action in {"list", "list_events"}
        ):
            return DeterministicToolSummary(
                True,
                calendar_list_summary(output),
            )
        if tool_name == "manage_tasks" and action == "list":
            text = str(output).strip()
            if text.startswith("AI: "):
                text = text[4:].strip()
            return DeterministicToolSummary(True, text)
        if tool_name in {"list_emails", "mcp__email__list_emails"}:
            return DeterministicToolSummary(
                True,
                email_list_summary(output),
            )
        if tool_name in {"read_email", "mcp__email__read_email"}:
            return DeterministicToolSummary(
                True,
                email_read_summary(output),
            )
    return DeterministicToolSummary(False)


def empty_response_fallback(
    full_response: str,
    round_reasoning: str,
    tool_events: list,
) -> tuple[str, str | None]:
    """Return a safe visible fallback without exposing private reasoning."""

    if full_response.strip() or tool_events:
        return full_response, None
    if round_reasoning.strip():
        message = "The model did not provide a final answer. Please try again."
        return (
            message,
            f'data: {json.dumps({"delta": message})}\n\n',
        )
    message = (
        "The model returned an empty response. Please try again or switch "
        "to a different model."
    )
    return message, f'data: {json.dumps({"delta": message})}\n\n'


__all__ = [
    "DeterministicToolSummary",
    "empty_response_fallback",
    "select_deterministic_tool_summary",
]
