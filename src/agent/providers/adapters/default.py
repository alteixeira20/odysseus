"""Default provider text normalization."""


def strip_think_blocks(text: str) -> str:
    """Remove closed literal think blocks with a forward-only scan."""

    if not text:
        return text
    lowered = text.lower()
    parts = []
    position = 0
    while True:
        start = lowered.find("<think>", position)
        if start == -1:
            parts.append(text[position:])
            break
        end = lowered.find("</think>", start + 7)
        if end == -1:
            parts.append(text[position:])
            break
        parts.append(text[position:start])
        position = end + 8
    return "".join(parts)
