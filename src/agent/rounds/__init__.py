"""Single-round stream parsing and projection components."""

from .document_stream import (
    normalize_odysseus_qwen_text,
    normalize_stream_document_fences,
    normalize_truncated_document_tool_fences,
    strip_document_model_artifacts,
)
from .tool_calls import resolve_tool_blocks
from .stream_consumer import stream_with_idle_status

__all__ = [
    "normalize_odysseus_qwen_text",
    "normalize_stream_document_fences",
    "normalize_truncated_document_tool_fences",
    "strip_document_model_artifacts",
    "resolve_tool_blocks",
    "stream_with_idle_status",
]
