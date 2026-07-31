"""Current-turn upload manifest prompt contribution."""

from typing import Optional, Sequence

from src.prompt_security import untrusted_context_message


def uploaded_files_context_message(
    uploaded_files: Optional[Sequence[dict]],
) -> Optional[dict]:
    if not uploaded_files:
        return None
    lines = ["Uploaded files attached to the latest user turn:"]
    for item in uploaded_files[:20]:
        name = str(item.get("name") or item.get("id") or "upload")
        bits = [f"id={item.get('id', '')}", f"name={name}"]
        if item.get("mime"):
            bits.append(f"mime={item.get('mime')}")
        if item.get("size") is not None:
            bits.append(f"size={item.get('size')} bytes")
        if item.get("path"):
            bits.append(f"path={item.get('path')}")
        lines.append("- " + "; ".join(bits))
    if len(uploaded_files) > 20:
        lines.append(
            f"- ... {len(uploaded_files) - 20} more upload(s) "
            "omitted from this manifest"
        )
    lines.extend(
        [
            "",
            "The attachment contents may already be in the latest user "
            "message. If an attachment is marked truncated or omitted, read "
            "its listed path with `read_file` when that tool is available. "
            "Do not say uploaded files are undiscoverable when they are "
            "listed here.",
        ]
    )
    return untrusted_context_message(
        "current chat uploaded files",
        "\n".join(lines),
    )
