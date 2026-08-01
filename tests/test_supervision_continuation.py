"""Unit coverage for src/agent/supervision/continuation.py — the extracted
truncation-continuation policy consulted by src/agent_loop.py's
`if not tool_blocks:` branch (see tests/test_agent_truncation_continuation.py
for the end-to-end version through the full agent loop).
"""

from src.agent.contracts import SupervisorAction
from src.agent.providers.finish_reason import ProviderFinished, ProviderFinishReason
from src.agent.supervision.continuation import (
    ContinuationDisposition,
    evaluate_truncation_continuation,
)


def _finished(**overrides):
    base = dict(
        raw_reason="length",
        normalized_reason=ProviderFinishReason.LENGTH,
        had_text=True,
        had_native_tool_call_fragment=False,
        had_complete_tool_call=False,
        had_incomplete_native_call=False,
        finish_event_seen=True,
    )
    base.update(overrides)
    return ProviderFinished(**base)


def test_length_truncation_yields_retry_decision():
    evaluation = evaluate_truncation_continuation(
        _finished(), continuation_count=0, force_answer=False
    )
    assert evaluation.disposition is ContinuationDisposition.RETRY
    decision = evaluation.decision
    assert decision is not None
    assert decision.action is SupervisorAction.RETRY_WITH_INSTRUCTION
    assert decision.instruction
    assert decision.metadata["attempt"] == 1
    assert decision.metadata["max_attempts"] == 4


def test_clean_stop_yields_no_decision():
    finished = _finished(raw_reason="stop", normalized_reason=ProviderFinishReason.STOP)
    assert evaluate_truncation_continuation(
        finished, continuation_count=0, force_answer=False
    ).disposition is ContinuationDisposition.NOT_TRUNCATED


def test_force_answer_round_never_continues_even_if_truncated():
    evaluation = evaluate_truncation_continuation(
        _finished(), continuation_count=0, force_answer=True
    )
    assert evaluation.disposition is ContinuationDisposition.UNSAFE_TO_RETRY
    assert evaluation.decision is None


def test_bounded_by_max_continuations():
    at_cap = evaluate_truncation_continuation(
        _finished(), continuation_count=4, force_answer=False, max_continuations=4
    )
    assert at_cap.disposition is ContinuationDisposition.RETRY_EXHAUSTED
    assert at_cap.decision is None

    under_cap = evaluate_truncation_continuation(
        _finished(), continuation_count=3, force_answer=False, max_continuations=4
    )
    assert under_cap.disposition is ContinuationDisposition.RETRY
    assert under_cap.decision.metadata["attempt"] == 4


def test_incomplete_native_call_without_length_still_continues():
    finished = _finished(
        raw_reason=None,
        normalized_reason=ProviderFinishReason.UNKNOWN,
        had_native_tool_call_fragment=True,
        had_incomplete_native_call=True,
    )
    evaluation = evaluate_truncation_continuation(
        finished, continuation_count=0, force_answer=False
    )
    assert evaluation.disposition is ContinuationDisposition.RETRY


def test_unclosed_fenced_call_still_continues():
    finished = _finished(
        raw_reason=None,
        normalized_reason=ProviderFinishReason.UNKNOWN,
        had_unclosed_fenced_call=True,
    )
    evaluation = evaluate_truncation_continuation(
        finished, continuation_count=0, force_answer=False
    )
    assert evaluation.disposition is ContinuationDisposition.RETRY
