"""Pure projections from tool results to agent text and frontend payloads."""

from dataclasses import dataclass
import json
import re
from typing import Any, Optional

from src.tool_utils import _truncate


DOCUMENT_TOOL_NAMES = frozenset(
    {
        "create_document",
        "update_document",
        "edit_document",
        "suggest_document",
    }
)


def resolved_tool_event_name(event: dict[str, Any]) -> str:
    """Resolve the concrete tool name represented by a persisted UI event."""

    tool = str(event.get("tool") or "").strip()
    if tool != "mcp":
        return tool
    for key in ("desc", "command", "output"):
        value = str(event.get(key) or "")
        match = re.search(r"\bmcp__[\w_]+\b", value)
        if match:
            return match.group(0)
    return tool


def extract_web_sources(result: dict[str, Any]) -> Optional[Any]:
    """Remove an embedded web source marker and return its decoded payload."""

    source_text = (
        result.get("output")
        or result.get("results")
        or result.get("stdout")
        or ""
    )
    if not source_text:
        return None
    marker = "<!-- SOURCES:"
    start = source_text.find(marker)
    if start < 0:
        return None
    end = source_text.find(" -->", start)
    if end < 0:
        return None
    try:
        sources = json.loads(source_text[start + len(marker) : end])
    except (json.JSONDecodeError, Exception):
        return None
    cleaned = source_text[:start].rstrip()
    for key in ("output", "results", "stdout"):
        if key in result:
            result[key] = cleaned
            break
    return sources


def document_result_payload(result: dict[str, Any]) -> Optional[dict[str, Any]]:
    if "action" not in result:
        return None
    if result["action"] == "suggest":
        return {
            "type": "doc_suggestions",
            "doc_id": result["doc_id"],
            "suggestions": result["suggestions"],
        }
    return {
        "type": "doc_update",
        "doc_id": result["doc_id"],
        "content": result["content"],
        "version": result["version"],
        "title": result.get("title", ""),
        "language": result.get("language"),
    }


def render_tool_output(
    result: dict[str, Any],
    *,
    is_document_tool: bool,
) -> str:
    """Render the exact compact output used by the frontend tool card."""

    if is_document_tool and "action" in result:
        action = result["action"]
        title = result.get("title", "")
        version = result.get("version", "?")
        if action == "create":
            return f'Document created: "{title}" (v{version})'
        if action == "edit":
            return (
                f'Document edited: "{title}" '
                f'(v{version}, {result.get("applied", 0)} edit(s))'
            )
        if action == "update":
            return f'Document updated: "{title}" (v{version})'
        return ""
    if result.get("timed_out"):
        parts = [str(result.get("error") or "Command timed out.")]
        if result.get("stdout"):
            parts.append("Partial stdout:\n" + str(result["stdout"]))
        if result.get("stderr"):
            parts.append("Partial stderr:\n" + str(result["stderr"]))
        return _truncate("\n\n".join(parts))
    if "stdout" in result:
        return _truncate(
            result["stdout"]
            or result["stderr"]
            or result.get("error", "")
        )
    if "output" in result:
        return _truncate(result["output"] or "")
    if "response" in result:
        label = result.get("model", result.get("session_name", "AI"))
        return _truncate(f"{label}: {result['response']}")
    if "content" in result:
        return _truncate(result["content"])
    if "results" in result:
        return _truncate(result["results"])
    if "session_id" in result and "name" in result:
        return (
            f"Session created: {result['name']} "
            f"(id: {result['session_id']})"
        )
    if "success" in result:
        return (
            f"Written: {result.get('path', '')}"
            if result["success"]
            else f"Error: {result.get('error', '')}"
        )
    if "error" in result:
        return _truncate(result["error"])
    return ""


def build_tool_output_payload(
    *,
    tool_name: str,
    command: str,
    output: str,
    result: dict[str, Any],
    invocation_id: str,
    is_document_tool: bool,
    ask_user: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    payload = {
        "type": "tool_output",
        "tool": tool_name,
        "command": command,
        "output": output,
        "exit_code": result.get("exit_code"),
        "invocation_id": result.get("invocation_id") or invocation_id,
    }
    for key in (
        "completion_state",
        "timed_out",
        "cancelled",
        "timeout_seconds",
        "error_type",
        "escalation",
        "shell_recovery",
        "partial_stdout",
        "partial_stderr",
    ):
        if key in result:
            payload[key] = result[key]
    if is_document_tool and "action" in result:
        payload.update(
            {
                "doc_id": result.get("doc_id"),
                "document_action": result.get("action"),
                "document_title": result.get("title", ""),
                "document_language": result.get("language", ""),
                "document_version": result.get("version"),
                "document_content": result.get("content", ""),
            }
        )
    if ask_user:
        payload["ask_user"] = ask_user
    if "ui_event" in result:
        payload["ui_event"] = result["ui_event"]
        for key in (
            "toggle_name",
            "state",
            "mode",
            "model",
            "endpoint_url",
            "theme_name",
            "colors",
            "uid",
            "folder",
            "account_id",
            "body",
            "panel",
        ):
            if key in result:
                payload[key] = result[key]
    for key in (
        "image_url",
        "image_id",
        "image_prompt",
        "image_model",
        "image_size",
        "image_quality",
    ):
        if key in result:
            payload[key] = result[key]
    if result.get("images"):
        image = result["images"][0]
        payload["screenshot"] = (
            f"data:{image['mimeType']};base64,{image['data']}"
        )
    if "diff" in result:
        payload["diff"] = result["diff"]
    return payload


def generated_image_payload(
    result: dict[str, Any],
) -> Optional[dict[str, Any]]:
    if not result.get("image_url"):
        return None
    payload = {
        "type": "generated_image",
        "url": result.get("image_url"),
    }
    for key in (
        "image_url",
        "image_id",
        "image_prompt",
        "image_model",
        "image_size",
        "image_quality",
    ):
        if key in result:
            payload[key] = result[key]
    return payload


@dataclass(frozen=True)
class ToolResultProjection:
    """All model, UI, and persistence projections for one tool result."""

    output_text: str
    before_summary_events: tuple[dict[str, Any], ...]
    after_summary_events: tuple[dict[str, Any], ...]
    question_delta: str
    note_anchor: str
    awaiting_user: bool
    ask_user: Optional[dict[str, Any]]
    tool_event: dict[str, Any]


def project_tool_result(
    *,
    tool_name: str,
    command: str,
    description: str,
    result: dict[str, Any],
    invocation_id: str,
    round_number: int,
    current_response: str,
) -> ToolResultProjection:
    """Build the ordered legacy projections for one completed tool call."""

    is_document_tool = tool_name in DOCUMENT_TOOL_NAMES
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []

    if tool_name == "web_search":
        sources = extract_web_sources(result)
        if sources is not None:
            before.append({"type": "web_sources", "data": sources})

    if is_document_tool and "action" in result:
        document_event = document_result_payload(result)
        if document_event:
            before.append(document_event)

    if "ui_event" in result:
        before.append({"type": "ui_control", "data": result})

    ask_user = result.get("ask_user")
    question_delta = ""
    if isinstance(ask_user, dict):
        question = str(ask_user.get("question") or "").strip()
        if question and question not in current_response:
            question_delta = (
                ("\n\n" if current_response.strip() else "") + question
            )
            before.append({"delta": question_delta})
    else:
        ask_user = None

    if "plan_update" in result:
        before.append(
            {"type": "plan_update", "data": result["plan_update"]}
        )

    output_text = render_tool_output(
        result,
        is_document_tool=is_document_tool,
    )
    before.append(
        build_tool_output_payload(
            tool_name=tool_name,
            command=command,
            output=output_text,
            result=result,
            invocation_id=invocation_id,
            is_document_tool=is_document_tool,
            ask_user=ask_user,
        )
    )
    image_event = generated_image_payload(result)
    if image_event:
        before.append(image_event)

    if ask_user:
        after.append({"type": "ask_user", "data": ask_user})

    if (
        tool_name
        in ("create_document", "update_document", "edit_document")
        and result.get("doc_id")
    ):
        after.append(
            {
                "type": "doc_update",
                "doc_id": result["doc_id"],
                "title": result.get("title", ""),
                "language": result.get("language", ""),
                "content": result.get("content", ""),
                "version": result.get("version", 1),
            }
        )

    research_session_id = result.get("research_session_id")
    if research_session_id:
        after.append(
            {
                "delta": (
                    "\n\n[Open in Deep Research]"
                    f"(#research-{research_session_id})\n"
                )
            }
        )

    note_anchor = ""
    note_id = result.get("note_id")
    if note_id and tool_name == "manage_notes":
        title = str(result.get("note_title") or "").strip()
        label = f"View note: {title}" if title else "View note"
        note_anchor = f"\n\n[{label}](#note-{note_id})\n"
        after.append({"delta": note_anchor})

    tool_event = {
        "round": round_number,
        "tool": resolved_tool_event_name(
            {
                "tool": tool_name,
                "desc": description,
                "command": command,
                "output": output_text,
            }
        ),
        "desc": description,
        "command": command,
        "output": output_text,
        "exit_code": result.get("exit_code"),
        "invocation_id": (
            result.get("invocation_id") or invocation_id
        ),
    }
    for key in (
        "completion_state",
        "timed_out",
        "cancelled",
        "timeout_seconds",
        "error_type",
        "escalation",
        "shell_recovery",
    ):
        if key in result:
            tool_event[key] = result[key]
    if result.get("image_url"):
        for key in (
            "image_url",
            "image_prompt",
            "image_model",
            "image_size",
            "image_quality",
        ):
            if result.get(key):
                tool_event[key] = result[key]
    if result.get("doc_id"):
        tool_event["doc_id"] = result["doc_id"]
        tool_event["doc_title"] = result.get("title", "")
    if result.get("diff"):
        tool_event["diff"] = result["diff"]
    if ask_user:
        tool_event["ask_user"] = ask_user

    return ToolResultProjection(
        output_text=output_text,
        before_summary_events=tuple(before),
        after_summary_events=tuple(after),
        question_delta=question_delta,
        note_anchor=note_anchor,
        awaiting_user=bool(ask_user),
        ask_user=ask_user,
        tool_event=tool_event,
    )


def note_list_summary(raw: str, max_items: int = 20) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    titles: list[str] = []
    for line in raw.splitlines():
        match = re.match(
            r"^\s*-\s+\[[^\]]+\]\s+\*\*(.*?)\*\*(.*)$",
            line,
        )
        if not match:
            continue
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        suffix = re.sub(r"\s+", " ", match.group(2) or "").strip()
        label = f"{title} {suffix}".strip()
        if label:
            titles.append(label)
        if len(titles) >= max_items:
            break
    if not titles:
        if re.search(r"\b(no notes|0 notes|found 0)\b", raw, re.IGNORECASE):
            return "No notes found."
        return ""
    total = len(
        re.findall(
            r"^\s*-\s+\[[^\]]+\]\s+\*\*",
            raw,
            re.MULTILINE,
        )
    )
    heading_count = total or len(titles)
    lines = [f"Here are your notes ({heading_count}):"]
    lines.extend(f"- {title}" for title in titles)
    if total and total > len(titles):
        lines.append(f"- ...and {total - len(titles)} more")
    return "\n".join(lines)


def calendar_list_summary(raw: str, max_items: int = 20) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    if re.search(r"\bno events between\b", raw, re.IGNORECASE):
        return raw.strip().splitlines()[0]
    items: list[str] = []
    for line in raw.splitlines():
        match = re.match(
            r"^\s*-\s+(.+?):\s+\[(.*?)\]\(#event-([^)]+)\)(.*)$",
            line,
        )
        if not match:
            continue
        when = re.sub(r"\s+", " ", match.group(1)).strip()
        title = re.sub(r"\s+", " ", match.group(2)).strip()
        suffix = re.sub(r"\s+", " ", match.group(4) or "").strip()
        label = f"{title} — {when}"
        if suffix:
            label += f" {suffix}"
        items.append(label)
        if len(items) >= max_items:
            break
    if not items:
        return ""
    total_match = re.search(r"Found\s+(\d+)\s+event", raw, re.IGNORECASE)
    total = int(total_match.group(1)) if total_match else len(items)
    lines = [f"Here are your events ({total}):"]
    lines.extend(f"- {item}" for item in items)
    if total > len(items):
        lines.append(f"- ...and {total - len(items)} more")
    return "\n".join(lines)


def _format_email_summary_item(item: dict[str, str]) -> str:
    subject = item.get("subject") or "(no subject)"
    parts = [subject]
    if item.get("from"):
        parts.append(f"from {item['from']}")
    if item.get("date"):
        parts.append(item["date"])
    if item.get("uid"):
        parts.append(f"UID {item['uid']}")
    text = " — ".join(parts)
    if item.get("summary"):
        text += f"\n  {item['summary']}"
    return text


def email_list_summary(raw: str, max_items: int = 10) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    if re.search(
        r"\b(no emails?|found 0 email|0 email)\b",
        raw,
        re.IGNORECASE,
    ):
        return "No emails found."
    items: list[str] = []
    current: Optional[dict[str, str]] = None
    for line in raw.splitlines():
        match = re.match(r"^\s*\d+\.\s+\*\*(.*?)\*\*\s*$", line)
        if match:
            if current:
                items.append(_format_email_summary_item(current))
                if len(items) >= max_items:
                    break
            current = {
                "subject": re.sub(r"\s+", " ", match.group(1)).strip()
            }
            continue
        if current is None:
            continue
        for field, pattern in (
            ("from", r"^\s*From:\s*(.+?)\s*$"),
            ("date", r"^\s*Date:\s*(.+?)\s*$"),
            ("uid", r"^\s*UID:\s*(.+?)\s*$"),
            ("summary", r"^\s*Summary:\s*(.+?)\s*$"),
        ):
            field_match = re.match(pattern, line)
            if field_match:
                current[field] = re.sub(
                    r"\s+",
                    " ",
                    field_match.group(1),
                ).strip()
                break
    if current and len(items) < max_items:
        items.append(_format_email_summary_item(current))
    if not items:
        return ""
    total_match = re.search(r"Found\s+(\d+)\s+email", raw, re.IGNORECASE)
    total = int(total_match.group(1)) if total_match else len(items)
    heading = (
        "Here is your latest email:"
        if total == 1
        else f"Here are your emails ({total}):"
    )
    lines = [heading]
    lines.extend(
        f"{index}. {item}"
        for index, item in enumerate(items, start=1)
    )
    if total > len(items):
        lines.append(f"- ...and {total - len(items)} more")
    return "\n".join(lines)


def email_read_summary(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return ""
    subject = sender = date = uid = ""
    body_lines: list[str] = []
    in_body = False
    for line in raw.splitlines():
        if line.strip() == "---":
            in_body = True
            continue
        if in_body:
            body_lines.append(line)
            continue
        for field, pattern in (
            ("subject", r"^\*\*Subject:\*\*\s*(.*)$"),
            ("sender", r"^\*\*From:\*\*\s*(.*)$"),
            ("date", r"^\*\*Date:\*\*\s*(.*)$"),
            ("uid", r"^\*\*UID:\*\*\s*(.*)$"),
        ):
            match = re.match(pattern, line)
            if not match:
                continue
            value = re.sub(r"\s+", " ", match.group(1)).strip()
            if field == "subject":
                subject = value
            elif field == "sender":
                sender = value
            elif field == "date":
                date = value
            else:
                uid = value
            break
    if not any((subject, sender, date, uid, body_lines)):
        return ""
    lines = [f"Email: {subject or '(no subject)'}"]
    if sender:
        lines.append(f"From: {sender}")
    if date:
        lines.append(f"Date: {date}")
    if uid:
        lines.append(f"UID: {uid}")
    body = "\n".join(body_lines).strip()
    if body:
        if len(body) > 1200:
            body = body[:1200].rstrip() + "\n..."
        lines.extend(("", body))
    return "\n".join(lines)
