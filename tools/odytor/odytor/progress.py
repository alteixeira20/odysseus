"""Lightweight progress/status output.

Status lines go to stderr so exported reports on stdout stay clean. Plain text
only: no spinners, no terminal control codes, no external dependencies. This
keeps output portable and safe to pipe or log.
"""

from __future__ import annotations

import sys
from typing import TextIO

PREFIX = "[odytor]"


class Progress:
    """A minimal status reporter. When disabled, every method is a no-op."""

    def __init__(self, enabled: bool = True, stream: TextIO | None = None) -> None:
        self.enabled = enabled
        self._stream = stream if stream is not None else sys.stderr

    def step(self, message: str) -> None:
        """Emit one status line, e.g. 'Fetching comments...'."""
        self._emit(message)

    def item(self, current: int, total: int | None, label: str) -> None:
        """Emit progress for a loop, e.g. 'Scanning 3/10 (30%)'."""
        if total:
            percent = round(current * 100 / total)
            self._emit(f"{label} {current}/{total} ({percent}%)")
        else:
            self._emit(f"{label} {current}")

    def page(self, label: str, number: int) -> None:
        """Emit progress for paginated data, e.g. 'Fetching comments page 2...'."""
        self._emit(f"{label} page {number}...")

    def done(self) -> None:
        self._emit("Done.")

    def _emit(self, message: str) -> None:
        if self.enabled:
            print(f"{PREFIX} {message}", file=self._stream, flush=True)


def disabled() -> Progress:
    """A no-op progress reporter, handy as a default argument."""
    return Progress(enabled=False)
