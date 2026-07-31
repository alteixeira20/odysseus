"""Shared field-naming convention for bounded tool results.

Root cause context: read_file/grep/glob/ls/bash already truncate oversized
output with a visible human-readable notice ("... [truncated at N
chars]") — they never silently drop data. What they lack is a *machine-
readable* continuation cursor: a model that hits a truncated read_file
result has no structured way to ask for "the rest" other than re-reading
from scratch, and nothing tells it the read was even incomplete without
parsing the notice text out of ``output``.

This module doesn't replace each tool's own truncation logic (still the
right place for it — grep's match-count cap, read_file's line-oriented
paging, and bash's raw byte cap are genuinely different shapes). It gives
them a shared minimal vocabulary to attach to their result dict so a
caller can always check ``result.get("truncated")`` rather than pattern-
matching output text: ``truncated``, ``returned_chars``, ``total_chars``,
and (only where the tool has a natural resume point) ``next_offset``.

The convention is wired into ``read_file`` (line offsets), repository
``grep``/``glob``/``ls`` results (result offsets), and ``bash``/``python``
(bounded stdout/stderr with total character counts). Repository result pages
include ``next_offset`` and a concrete resume hint instead of requiring the
model to repeat a full search and parse a prose-only cap notice.
"""

from __future__ import annotations

from typing import Optional


def bounded_char_result(
    text: str,
    max_chars: int,
    *,
    notice_template: str = "\n... [truncated at {max_chars} chars]",
) -> dict:
    """Cap ``text`` at ``max_chars``, returning the standard bounded-result shape.

    Keeps the head (matches every existing tool's truncation behavior —
    changing to head+tail here would alter established output shape for
    no clear benefit on structured/log-like tool output).
    """
    total_chars = len(text) if isinstance(text, str) else 0
    if not isinstance(text, str) or total_chars <= max_chars:
        return {
            "output": text,
            "truncated": False,
            "returned_chars": total_chars,
            "total_chars": total_chars,
        }
    notice = notice_template.format(max_chars=max_chars)
    kept = text[:max_chars]
    return {
        "output": kept + notice,
        "truncated": True,
        "returned_chars": len(kept),
        "total_chars": total_chars,
    }


def line_read_cursor(
    *,
    truncated: bool,
    last_line_read: int,
    limit: int,
    hit_eof: bool,
) -> Optional[int]:
    """Compute the ``next_offset`` (1-based line number) for a paged file read.

    None when there's nothing left to read (truncation was char-budget
    driven right at EOF, or the read reached EOF without hitting a limit).
    """
    if hit_eof and not truncated:
        return None
    if not truncated and limit <= 0:
        return None
    return last_line_read + 1
