"""Email-draft prompt context normalization."""


def compact_email_draft_context(
    raw: str,
    *,
    max_own_chars: int = 1200,
    max_history_chars: int = 1200,
) -> str:
    text = raw or ""
    if "\n---\n" not in text:
        return text[:3500] + (
            "\n...[truncated]" if len(text) > 3500 else ""
        )
    header, body = text.split("\n---\n", 1)
    literal = "---------- Previous message ----------"
    index = body.find(literal)
    if index >= 0:
        own = body[:index].strip()
        history = body[index:].strip()
    else:
        own = body.strip()
        history = ""
    if len(own) > max_own_chars:
        own = (
            own[:max_own_chars].rstrip()
            + "\n...[draft body truncated]"
        )
    if len(history) > max_history_chars:
        history = (
            history[:max_history_chars].rstrip()
            + "\n...[quoted history truncated; full history is preserved by "
            "Odysseus]"
        )
    if history:
        body_out = (f"{own}\n\n" if own else "") + (
            "QUOTED HISTORY EXCERPT FOR CONTEXT ONLY -- do not rewrite or "
            "include this excerpt in your tool output; Odysseus preserves "
            "the full quoted thread below the reply automatically.\n"
            f"{history}"
        )
    else:
        body_out = own
    return header.rstrip() + "\n---\n" + body_out.strip()
