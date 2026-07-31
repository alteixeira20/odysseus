"""Normalized provider termination contract.

Root cause context: the OpenAI-compatible and Ollama-native streaming paths in
``src/llm_core.py`` never read the provider's per-choice ``finish_reason`` (or
Ollama's ``done_reason``) field. A round that ended because the provider hit
its output-token limit was therefore indistinguishable from one that ended
because the model deliberately stopped — both looked like "the stream closed
after some text", and the agent loop's ``if not tool_blocks: ... break # no
tools — done`` fallback (src/agent_loop.py) treated the truncated text as a
final answer. This module gives the rest of the runtime a typed vocabulary to
tell those two cases apart; ``src/llm_core.py`` populates it from the wire,
``src/agent/rounds/provider_events.py`` accumulates it per round, and
``src/agent_loop.py`` consults it before accepting a tool-free round as done.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.agent.providers.termination import RECOVERABLE_KINDS, StreamTermination


class ProviderFinishReason(str, Enum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    CANCELLED = "cancelled"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    UNKNOWN = "unknown"


_RAW_TO_NORMALIZED = {
    "stop": ProviderFinishReason.STOP,
    "eos": ProviderFinishReason.STOP,
    "end_turn": ProviderFinishReason.STOP,
    "stop_sequence": ProviderFinishReason.STOP,
    "tool_calls": ProviderFinishReason.TOOL_CALLS,
    "function_call": ProviderFinishReason.TOOL_CALLS,
    "tool_use": ProviderFinishReason.TOOL_CALLS,
    "length": ProviderFinishReason.LENGTH,
    "max_tokens": ProviderFinishReason.LENGTH,
    "model_length": ProviderFinishReason.LENGTH,
    # ChatGPT Subscription / Codex Responses API: response.status == "incomplete"
    # carries incomplete_details.reason as one of these two values.
    "max_output_tokens": ProviderFinishReason.LENGTH,
    "incomplete": ProviderFinishReason.LENGTH,
    "content_filter": ProviderFinishReason.CONTENT_FILTER,
    "safety": ProviderFinishReason.CONTENT_FILTER,
}


def normalize_finish_reason(raw: Optional[str]) -> ProviderFinishReason:
    """Map a provider's raw finish-reason string onto the normalized contract.

    A missing/unrecognized value normalizes to UNKNOWN, never STOP — the
    runtime must never infer intentional completion from an absent field.
    """
    if not raw or not isinstance(raw, str):
        return ProviderFinishReason.UNKNOWN
    return _RAW_TO_NORMALIZED.get(raw.strip().lower(), ProviderFinishReason.UNKNOWN)


@dataclass(frozen=True)
class ProviderFinished:
    """Terminal metadata for one provider round, captured once per attempt."""

    raw_reason: Optional[str]
    normalized_reason: ProviderFinishReason
    had_text: bool
    had_native_tool_call_fragment: bool
    had_complete_tool_call: bool
    had_incomplete_native_call: bool
    finish_event_seen: bool
    had_unclosed_fenced_call: bool = False
    termination: Optional[StreamTermination] = None


def classify_truncation(finished: ProviderFinished) -> bool:
    """True when a tool-free round must not be accepted as a final answer.

    Deliberately conservative: relies on provider metadata and parser state
    first (LENGTH, or a native call whose JSON never closed), not on text
    heuristics. Callers may still layer a bounded text heuristic (e.g. the
    existing intent-without-action supervisor) on top for the case where the
    provider omits finish_reason entirely and a native call was never opened.

    A recoverable transport/protocol interruption (see
    ``src/agent/providers/termination.py``) is treated the same as a
    provider-reported LENGTH truncation: the round produced no complete tool
    call, so resuming it is side-effect-free by construction.
    """
    if finished.normalized_reason is ProviderFinishReason.LENGTH:
        return True
    if finished.had_incomplete_native_call:
        return True
    if finished.had_unclosed_fenced_call:
        return True
    if finished.termination is not None and finished.termination.recoverable:
        return True
    return False


def is_interruption_only(finished: ProviderFinished) -> bool:
    """True when the sole reason for truncation is a transport interruption.

    Used to pick continuation-instruction wording: an interrupted stream
    should not be told it "reached its output limit" when it didn't.
    """
    if finished.normalized_reason is ProviderFinishReason.LENGTH:
        return False
    if finished.had_incomplete_native_call or finished.had_unclosed_fenced_call:
        return False
    return finished.termination is not None and finished.termination.recoverable


__all__ = [
    "ProviderFinishReason",
    "ProviderFinished",
    "classify_truncation",
    "is_interruption_only",
    "normalize_finish_reason",
    "RECOVERABLE_KINDS",
]
