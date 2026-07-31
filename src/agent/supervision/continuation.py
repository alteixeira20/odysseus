"""Decide whether a truncated, tool-free round should be safely resumed.

A tool-free round is not automatically a deliberate final answer — the
provider may have hit its output-token limit mid-sentence or mid tool-call
(see ``src/agent/providers/finish_reason.py``). This module owns the policy
that turns that classification into a bounded, side-effect-free retry
decision; ``src/agent_loop.py`` only calls it and projects the result onto
the wire (SSE event + appended message + loop continuation).
"""

from __future__ import annotations

from typing import Optional

from src.agent.contracts import SupervisorAction, SupervisorDecision
from src.agent.providers.finish_reason import (
    ProviderFinished,
    classify_truncation,
    is_interruption_only,
)

# Cap on automatic truncation continuations per run. Bounds a model whose
# provider keeps truncating (e.g. a persistently too-low max_tokens config)
# from silently consuming the entire round budget on retries.
DEFAULT_MAX_TRUNCATION_CONTINUATIONS = 4

_CONTINUATION_INSTRUCTION = (
    "Your previous output was cut off because the "
    "provider reached its output limit before "
    "finishing. Continue immediately from exactly "
    "where you left off — do not repeat what you "
    "already wrote and do not summarize the turn "
    "again. If the next step requires a tool, emit "
    "the tool call immediately with no "
    "announcement-only sentence first."
)

_INTERRUPTION_INSTRUCTION = (
    "Your previous response was cut off by an unexpected "
    "connection interruption, not a deliberate stop. "
    "Continue immediately from exactly where you left off — "
    "do not repeat what you already wrote and do not restart "
    "or re-summarize the turn. If the next step requires a "
    "tool, emit the tool call immediately with no "
    "announcement-only sentence first."
)


def evaluate_truncation_continuation(
    finished: ProviderFinished,
    *,
    continuation_count: int,
    force_answer: bool,
    max_continuations: int = DEFAULT_MAX_TRUNCATION_CONTINUATIONS,
) -> Optional[SupervisorDecision]:
    """Return a retry decision, or None if this round is not a safe truncation retry.

    Callers must only invoke this for a round where no tool ran (``not
    tool_blocks``) — that is what makes the retry side-effect-free by
    construction: there is nothing a duplicate execution could repeat.

    Returns None when: the round is on a forced-answer path (no more tool
    turns to give), the round isn't classified as truncated, or the bounded
    retry budget for this run is already spent.
    """
    if force_answer:
        return None
    if not classify_truncation(finished):
        return None
    if continuation_count >= max_continuations:
        return None
    interruption_only = is_interruption_only(finished)
    return SupervisorDecision(
        action=SupervisorAction.RETRY_WITH_INSTRUCTION,
        reason=(
            "transport_interruption_continuation"
            if interruption_only
            else "truncation_continuation"
        ),
        instruction=(
            _INTERRUPTION_INSTRUCTION
            if interruption_only
            else _CONTINUATION_INSTRUCTION
        ),
        metadata={
            "attempt": continuation_count + 1,
            "max_attempts": max_continuations,
            "normalized_reason": finished.normalized_reason.value,
            "raw_reason": finished.raw_reason,
            "cause": (
                "transport_interruption" if interruption_only else "output_limit"
            ),
            "termination_kind": (
                finished.termination.kind.value if finished.termination else None
            ),
        },
    )
