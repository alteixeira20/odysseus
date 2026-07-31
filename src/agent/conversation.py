"""Pure conversation assembly primitives used by the agent runtime."""

from typing import Dict, List, Optional


def extract_last_user_message(messages: List[Dict]) -> str:
    """Return the most recent user message as plain text."""

    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict)
                )
            return content
    return ""


def user_turn_count(messages: Optional[List[Dict]]) -> int:
    """Count real user turns in the message list."""

    count = 0
    for msg in messages or []:
        if msg.get("role") == "user":
            count += 1
    return count


def insert_before_latest_user(
    messages: Optional[List[Dict]],
    context_msg: Dict,
) -> List[Dict]:
    """Insert a context message immediately before the latest user turn."""

    out = list(messages or [])
    for idx in range(len(out) - 1, -1, -1):
        if out[idx].get("role") == "user":
            out.insert(idx, context_msg)
            return out
    out.append(context_msg)
    return out


def recent_context_for_retrieval(
    messages: List[Dict],
    max_user: int = 3,
    max_chars: int = 600,
) -> str:
    """Build a newest-first tool-retrieval query from recent human turns."""

    collected = []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict)
            )
        content = (content or "").strip()
        meta = msg.get("metadata") or {}
        if (
            not content
            or meta.get("trusted") is False
            or content.startswith("[Tool execution results]")
        ):
            continue
        collected.append(content)
        if len(collected) >= max_user:
            break
    return "\n".join(collected)[:max_chars]
