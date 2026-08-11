"""Compatibility façade for the pre-``AgentRunner`` Runtime V3 entry point.

Canonical execution uses :class:`src.agent.runtime_v3.lifecycle.DurableRunLifecycle`
directly from ``AgentRunner``.  This function remains for characterized tests
and transitional callers; it no longer owns an independent orchestration path.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Callable

from .lifecycle import (
    DurableRunLifecycle,
    event_type_from_wire,
    terminal_from_legacy_wire,
)


# Preserve private names used by historical diagnostics/tests while making the
# implementation source explicit.
_event_type = event_type_from_wire
_terminal_from_wire = terminal_from_legacy_wire


async def stream_with_durable_runtime(
    request,
    legacy_factory: Callable[[], Any],
) -> AsyncGenerator[str, None]:
    """Delegate the historical wrapper API to the canonical lifecycle service."""

    lifecycle = DurableRunLifecycle()
    async for event in lifecycle.stream(request, legacy_factory):
        yield event


__all__ = ["stream_with_durable_runtime"]
