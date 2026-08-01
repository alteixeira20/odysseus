"""Typed process-execution authority carried by one agent run.

Workspace identity and process authority are deliberately independent.  A
workspace selects a working tree; it never grants either execution mode.
Legacy callers that only send ``allow_bash=true`` receive the safer sandboxed
mode.  Host execution is available only through the explicit ``shell_mode``
field and must still pass route privilege checks.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ExecutionMode(str, Enum):
    DISABLED = "disabled"
    SANDBOXED = "sandboxed"
    HOST = "host"

    @property
    def enabled(self) -> bool:
        return self is not ExecutionMode.DISABLED


def resolve_execution_mode(
    shell_mode: Any,
    *,
    allow_bash: Any = None,
) -> ExecutionMode:
    """Resolve request fields without allowing legacy values to grant host.

    An explicit/missing ``allow_bash`` denial always wins.  This retains the
    existing immutable-denial contract while giving new clients a typed mode.
    ``allow_bash=true`` with no mode is the backward-compatible sandbox grant.
    """

    if str(allow_bash).strip().lower() != "true":
        return ExecutionMode.DISABLED
    normalized = str(shell_mode or "").strip().lower()
    if normalized == ExecutionMode.HOST.value:
        return ExecutionMode.HOST
    if normalized in {"", ExecutionMode.SANDBOXED.value}:
        return ExecutionMode.SANDBOXED
    return ExecutionMode.DISABLED


def normalize_execution_mode(
    value: Any,
    *,
    shell_enabled: Any = None,
) -> ExecutionMode:
    """Normalize trusted internal/legacy arguments.

    Direct Python callers may pass an enum/string.  The old ``shell_enabled``
    flag can opt into sandboxing, never host access.
    """

    if isinstance(value, ExecutionMode):
        return value
    normalized = str(value or "").strip().lower()
    try:
        return ExecutionMode(normalized)
    except ValueError:
        return (
            ExecutionMode.SANDBOXED
            if shell_enabled is True
            else ExecutionMode.DISABLED
        )


__all__ = [
    "ExecutionMode",
    "normalize_execution_mode",
    "resolve_execution_mode",
]
