"""Provider-independent projection of streamed document tool output."""

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Iterable

from src.agent.events import AgentEvent


_DOCUMENT_MODEL_ARTIFACT_RE = re.compile(
    r"(?:\|end\|)+\|?assistan(?:t)?\|?"
    r"|\|assistan(?:t)?\|"
    r"|<\|im_start\|>\s*assistant"
    r"|<\|im_end\|>",
    re.IGNORECASE,
)

_ODYSSEUS_QWEN_TEXT_FIXES = (
    (re.compile(r"\bassistan\b", re.IGNORECASE), "assistant"),
    (re.compile(r"\bdon'\b", re.IGNORECASE), "don't"),
    (re.compile(r"\bcan'\b", re.IGNORECASE), "can't"),
    (re.compile(r"\bwon'\b", re.IGNORECASE), "won't"),
    (re.compile(r"\blates\b", re.IGNORECASE), "latest"),
    (re.compile(r"\baccoun\b", re.IGNORECASE), "account"),
    (re.compile(r"\bconten\b", re.IGNORECASE), "content"),
    (re.compile(r"\bdocumen\b", re.IGNORECASE), "document"),
    (re.compile(r"\breques\b", re.IGNORECASE), "request"),
    (re.compile(r"\bnex\b", re.IGNORECASE), "next"),
    (re.compile(r"\btex\b", re.IGNORECASE), "text"),
    (re.compile(r"\bsen\b", re.IGNORECASE), "sent"),
    (re.compile(r"\bsecre\b", re.IGNORECASE), "secret"),
    (re.compile(r"\bAnalys\b"), "Analyst"),
    (re.compile(r"\bAugus\b"), "August"),
    (re.compile(r"\bbu\b", re.IGNORECASE), "but"),
    (re.compile(r"\bmigh\b", re.IGNORECASE), "might"),
    (re.compile(r"\bdifferen\b", re.IGNORECASE), "different"),
    (re.compile(r"\bpoin\b", re.IGNORECASE), "point"),
    (re.compile(r"\bmos\b", re.IGNORECASE), "most"),
    (re.compile(r"\bjus\b", re.IGNORECASE), "just"),
    (re.compile(r"\bBes\b"), "Best"),
    (re.compile(r"\bstar\b", re.IGNORECASE), "start"),
    (re.compile(r"\bge\b", re.IGNORECASE), "get"),
    (re.compile(r"\ble\b", re.IGNORECASE), "let"),
    (re.compile(r"\bwha\b", re.IGNORECASE), "what"),
    (re.compile(r"\btha\b", re.IGNORECASE), "that"),
)

_TRUNCATED_DOCUMENT_FENCE_RE = re.compile(
    r"```(create|update|edit|edi|suggest)_documen(?!t)(?=\s|\n|```)",
    re.IGNORECASE,
)

_COMPACT_DOCUMENT_MARKERS = {
    "<<FIND>": "<<<FIND>>>",
    "<<REPLACE>": "<<<REPLACE>>>",
    "<<SUGGEST>": "<<<SUGGEST>>>",
    "<<REASON>": "<<<REASON>>>",
    "<<END>": "<<<END>>>",
}

_DOCUMENT_LANGUAGES = {
    "python",
    "py",
    "javascript",
    "js",
    "typescript",
    "ts",
    "html",
    "css",
    "json",
    "yaml",
    "bash",
    "sql",
    "rust",
    "go",
    "java",
    "c",
    "cpp",
    "markdown",
    "text",
}


def _decode_partial_json_string(raw: str) -> str:
    try:
        return json.loads('"' + raw + '"')
    except Exception:
        try:
            return json.loads('"' + raw.rstrip("\\") + '"')
        except Exception:
            return (
                raw.replace("\\n", "\n")
                .replace("\\t", "\t")
                .replace('\\"', '"')
                .replace("\\\\", "\\")
            )


@dataclass
class DocumentStreamProjector:
    """Project native argument and fenced-text deltas onto document UI events.

    One instance owns exactly one provider round. The caller retains ownership
    of provider parsing, tool policy, tool-call collection, and SSE encoding.
    """

    odysseus_create_mode: bool = False
    accumulated_arguments: str = ""
    opened: bool = False
    last_content_length: int = 0
    fence_offset: int = 0
    scan_from: int = 0

    def consume_native_argument_delta(
        self,
        argument_delta: str,
    ) -> tuple[AgentEvent, ...]:
        """Consume one native tool argument fragment."""

        self.accumulated_arguments += argument_delta or ""
        events: list[AgentEvent] = []

        if not self.opened:
            title_match = re.search(
                r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"',
                self.accumulated_arguments,
            )
            if title_match:
                self.opened = True
                title = _decode_partial_json_string(title_match.group(1))
                language_match = re.search(
                    r'"language"\s*:\s*"((?:[^"\\]|\\.)*)"',
                    self.accumulated_arguments,
                )
                language = (
                    _decode_partial_json_string(language_match.group(1))
                    if language_match
                    else ""
                )
                events.append(
                    AgentEvent.typed(
                        "doc_stream_open",
                        title=title,
                        language=language,
                    )
                )

        if self.opened:
            content_match = re.search(
                r'"content"\s*:\s*"',
                self.accumulated_arguments,
            )
            if content_match:
                raw = self.accumulated_arguments[content_match.end() :]
                raw = re.sub(r'"\s*\}\s*$', "", raw)
                decoded = _decode_partial_json_string(raw)
                if len(decoded) > self.last_content_length:
                    self.last_content_length = len(decoded)
                    events.append(
                        AgentEvent.typed(
                            "doc_stream_delta",
                            content=decoded,
                        )
                    )

        return tuple(events)

    def preview_fenced_tool_blocks(
        self,
        tool_blocks: Iterable[Any],
        *,
        round_number: int,
        is_blocked: Callable[[str], bool],
    ) -> tuple[AgentEvent, ...]:
        """Project the legacy pre-execution fenced document preview."""

        blocks = list(tool_blocks)
        if not self.opened and round_number == 1:
            for block in blocks:
                if is_blocked(block.tool_type):
                    continue
                if block.tool_type == "create_document":
                    self.opened = True
                    break

        if self.opened:
            return ()

        for block in blocks:
            if is_blocked(block.tool_type):
                continue
            if block.tool_type == "create_document":
                lines = block.content.strip().split("\n")
                title = lines[0].strip() if lines else "Untitled"
                language = ""
                content_start = 1
                if (
                    len(lines) > 1
                    and len(lines[1].strip()) < 20
                    and lines[1].strip().isalpha()
                ):
                    language = lines[1].strip()
                    content_start = 2
                content = (
                    "\n".join(lines[content_start:])
                    if len(lines) > content_start
                    else ""
                )
                events = [
                    AgentEvent.typed(
                        "doc_stream_open",
                        title=title,
                        language=language,
                    )
                ]
                if content:
                    events.append(
                        AgentEvent.typed(
                            "doc_stream_delta",
                            content=content,
                        )
                    )
                return tuple(events)
            if block.tool_type == "update_document":
                return (
                    AgentEvent.typed(
                        "doc_stream_open",
                        title="",
                        language="",
                    ),
                    AgentEvent.typed(
                        "doc_stream_delta",
                        content=block.content.strip(),
                    ),
                )
        return ()

    def consume_visible_text(
        self,
        round_response: str,
        *,
        round_number: int,
        create_document_blocked: bool = False,
    ) -> tuple[AgentEvent, ...]:
        """Project a growing fenced document response onto UI events."""

        if (
            (round_number <= 1 and not self.odysseus_create_mode)
            or self.accumulated_arguments
            or create_document_blocked
        ):
            return ()

        markers = (
            ("```document\n", "```documen\n")
            if self.odysseus_create_mode
            else ("```create_document\n",)
        )
        fence_marker = next(
            (
                marker
                for marker in markers
                if marker in round_response[self.scan_from :]
            ),
            None,
        )
        events: list[AgentEvent] = []

        if not self.opened and fence_marker:
            fence_index = round_response.index(fence_marker, self.scan_from)
            after_fence = round_response[fence_index + len(fence_marker) :]
            lines = after_fence.split("\n")
            if lines and lines[0].strip():
                self.opened = True
                title = lines[0].strip()
                language = (
                    lines[1].strip()
                    if len(lines) > 1
                    and lines[1].strip().lower() in _DOCUMENT_LANGUAGES
                    else ""
                )
                self.fence_offset = (
                    fence_index + len(fence_marker) + len(lines[0]) + 1
                )
                if language:
                    self.fence_offset += len(lines[1]) + 1
                self.last_content_length = 0
                events.append(
                    AgentEvent.typed(
                        "doc_stream_open",
                        title=title,
                        language=language,
                    )
                )

        if self.opened:
            content = round_response[self.fence_offset :]
            closing_index = content.find("\n```")
            if closing_index >= 0:
                content = content[:closing_index]
            if len(content) > self.last_content_length:
                self.last_content_length = len(content)
                events.append(
                    AgentEvent.typed(
                        "doc_stream_delta",
                        content=content,
                    )
                )
            if closing_index >= 0:
                self.opened = False
                self.scan_from = (
                    self.fence_offset + closing_index + len("\n```")
                )
                self.fence_offset = 0
                self.last_content_length = 0

        return tuple(events)


def strip_document_model_artifacts(text: str) -> str:
    return _DOCUMENT_MODEL_ARTIFACT_RE.sub("", text or "")


def normalize_odysseus_qwen_text(text: str) -> str:
    """Repair high-confidence dropped-final-letter artifacts."""

    if not text:
        return text
    fixed = text
    for pattern, replacement in _ODYSSEUS_QWEN_TEXT_FIXES:
        fixed = pattern.sub(replacement, fixed)
    return fixed


def normalize_truncated_document_tool_fences(text: str) -> str:
    """Repair truncated document tool tags and compact edit markers."""

    normalized = _TRUNCATED_DOCUMENT_FENCE_RE.sub(
        lambda match: (
            f"```{'edit' if match.group(1).lower() == 'edi' else match.group(1).lower()}_document"
        ),
        text or "",
    )
    for compact, full in _COMPACT_DOCUMENT_MARKERS.items():
        normalized = normalized.replace(compact, full)
    marker = r"<<<(?:FIND|REPLACE|SUGGEST|REASON|END)>>>"
    normalized = re.sub(rf"(?<!\n)({marker})", r"\n\1", normalized)
    normalized = re.sub(rf"({marker})(?=\S)", r"\1\n", normalized)
    normalized = re.sub(
        r"(<<<(?:REPLACE|SUGGEST|REASON)>>>)\n(<<<END>>>)",
        r"\1\n\n\2",
        normalized,
    )
    normalized = re.sub(r"\n(```)", r"\1", normalized)
    return normalized


def normalize_stream_document_fences(
    text: str,
    target_tool: str = "create_document",
) -> str:
    """Map neutral document fences onto the active document operation."""

    text = normalize_truncated_document_tool_fences(
        strip_document_model_artifacts(text or "")
    )

    def replace(match: re.Match) -> str:
        body = match.group(1) or ""
        if target_tool == "update_document":
            lines = body.splitlines()
            if lines and not lines[0].lstrip().startswith("#"):
                lines = lines[1:]
            if lines and lines[0].strip().lower() in {
                "markdown",
                "md",
                "text",
                "txt",
                "html",
                "email",
                "python",
                "javascript",
                "typescript",
                "json",
                "yaml",
            }:
                lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
            body = "\n".join(lines)
        return f"```{target_tool}\n{body}"

    return re.sub(
        r"```documen(?:t)?\s*\n([\s\S]*?)(?=\n```|$)",
        replace,
        text,
        flags=re.IGNORECASE,
    )
