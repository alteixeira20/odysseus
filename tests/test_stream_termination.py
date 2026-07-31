"""Pure classification coverage for src/agent/providers/termination.py.

See that module's docstring for the root-cause context: src/llm_core.py's
streaming branches could not previously distinguish "the provider sent its
own terminal signal" from "the HTTP iterator just ran out of lines", nor
tag a transport-layer failure distinctly from a hard provider error. These
tests exercise the classification matrix in isolation, independent of any
provider wiring.
"""

from src.agent.providers.termination import (
    StreamTermination,
    StreamTerminationKind,
    classify_stream_termination,
    error_kind_to_termination_kind,
)


def _base(**overrides):
    base = dict(
        provider_finish_seen=True,
        protocol_done_seen=True,
        normalized_reason_is_known=True,
    )
    base.update(overrides)
    return classify_stream_termination(**base)


def test_explicit_provider_reason_is_provider_terminal():
    t = _base()
    assert t.kind is StreamTerminationKind.PROVIDER_TERMINAL
    assert t.recoverable is False


def test_protocol_done_without_reason_is_not_recoverable():
    # e.g. an explicit [DONE] sentinel arrived but the provider never
    # populated finish_reason on any chunk — legitimate protocol
    # completion, not an interruption.
    t = _base(normalized_reason_is_known=False)
    assert t.kind is StreamTerminationKind.PROTOCOL_DONE
    assert t.recoverable is False


def test_no_finish_event_at_all_is_transport_eof_and_recoverable():
    # e.g. the pre-fix Ollama branch: the loop ended without ever emitting
    # a finish event.
    t = _base(
        provider_finish_seen=False,
        protocol_done_seen=False,
        normalized_reason_is_known=False,
    )
    assert t.kind is StreamTerminationKind.TRANSPORT_EOF
    assert t.recoverable is True


def test_synthesized_finish_without_protocol_terminal_is_transport_eof():
    # A finish event WAS emitted (reason=None) but no explicit protocol
    # terminal (no [DONE], no done:true, no message_stop, ...) was ever
    # observed — the generator's read loop simply ran out of lines.
    t = _base(
        provider_finish_seen=True,
        protocol_done_seen=False,
        normalized_reason_is_known=False,
    )
    assert t.kind is StreamTerminationKind.TRANSPORT_EOF
    assert t.recoverable is True


def test_recoverable_transport_eof_with_complete_tool_call_is_not_recoverable():
    # A complete tool call must be executed or discarded by the normal
    # tool-call path, never silently re-requested from the provider.
    t = _base(
        provider_finish_seen=False,
        protocol_done_seen=False,
        normalized_reason_is_known=False,
        had_complete_tool_call=True,
    )
    assert t.kind is StreamTerminationKind.TRANSPORT_EOF
    assert t.recoverable is False


def test_connect_error_kind_is_transport_error_and_recoverable():
    t = _base(error_kind="connect")
    assert t.kind is StreamTerminationKind.TRANSPORT_ERROR
    assert t.recoverable is True


def test_read_timeout_error_kind_is_timeout_and_recoverable():
    t = _base(error_kind="read_timeout")
    assert t.kind is StreamTerminationKind.TIMEOUT
    assert t.recoverable is True


def test_network_error_kind_is_transport_error():
    t = _base(error_kind="network")
    assert t.kind is StreamTerminationKind.TRANSPORT_ERROR


def test_protocol_error_kind_is_transport_error():
    # e.g. httpx.ProtocolError / RemoteProtocolError — malformed or
    # truncated chunked body, a textbook mid-stream interruption.
    t = _base(error_kind="protocol")
    assert t.kind is StreamTerminationKind.TRANSPORT_ERROR


def test_unknown_exception_error_kind_is_transport_error():
    t = _base(error_kind="unknown_exception")
    assert t.kind is StreamTerminationKind.TRANSPORT_ERROR


def test_unrecognized_error_kind_string_falls_through_to_other_signals():
    # A future error_kind value this module doesn't know about must not be
    # silently treated as recoverable — it falls through the precedence
    # chain instead of being force-mapped.
    t = _base(error_kind="something_new", normalized_reason_is_known=True)
    assert t.kind is StreamTerminationKind.PROVIDER_TERMINAL


def test_cancellation_always_wins_and_is_never_recoverable():
    t = _base(error_kind="network", cancelled=True)
    assert t.kind is StreamTerminationKind.CANCELLED
    assert t.recoverable is False


def test_cancellation_wins_even_with_substantive_content():
    t = _base(
        cancelled=True,
        had_visible_text=True,
        provider_finish_seen=False,
        protocol_done_seen=False,
    )
    assert t.kind is StreamTerminationKind.CANCELLED
    assert t.recoverable is False


def test_deadline_exceeded_is_timeout_and_recoverable():
    t = _base(
        deadline_exceeded=True,
        provider_finish_seen=False,
        protocol_done_seen=False,
        normalized_reason_is_known=False,
    )
    assert t.kind is StreamTerminationKind.TIMEOUT
    assert t.recoverable is True


def test_deadline_exceeded_takes_precedence_over_missing_finish_event():
    # Even if a finish event happened to arrive, an explicit deadline
    # cutoff is the more specific/actionable signal.
    t = _base(deadline_exceeded=True)
    assert t.kind is StreamTerminationKind.TIMEOUT


def test_parse_error_is_recognized_and_not_recoverable():
    t = _base(parse_error=True, provider_finish_seen=False, protocol_done_seen=False)
    assert t.kind is StreamTerminationKind.PARSE_ERROR
    assert t.recoverable is False


def test_error_kind_to_termination_kind_unmapped_returns_none():
    assert error_kind_to_termination_kind(None) is None
    assert error_kind_to_termination_kind("") is None
    assert error_kind_to_termination_kind("some_future_kind") is None


def test_error_kind_to_termination_kind_known_values():
    assert error_kind_to_termination_kind("connect") is StreamTerminationKind.TRANSPORT_ERROR
    assert error_kind_to_termination_kind("read_timeout") is StreamTerminationKind.TIMEOUT


def test_termination_precedence_error_over_missing_protocol_done():
    # A transport error tag must win over the generic "no protocol done
    # seen" fallback, even though both would independently suggest a
    # recoverable interruption — the error_kind is the more specific cause.
    t = _base(
        error_kind="read_timeout",
        provider_finish_seen=False,
        protocol_done_seen=False,
        normalized_reason_is_known=False,
    )
    assert t.kind is StreamTerminationKind.TIMEOUT


def test_stream_termination_is_frozen_and_carries_diagnostics():
    t = StreamTermination(
        kind=StreamTerminationKind.TRANSPORT_ERROR,
        provider_finish_seen=False,
        protocol_done_seen=False,
        transport_closed_cleanly=False,
        had_visible_text=True,
        had_reasoning=False,
        had_tool_call_fragment=False,
        had_complete_tool_call=False,
        error_type="network",
    )
    assert t.recoverable is True
    try:
        t.kind = StreamTerminationKind.PROVIDER_TERMINAL  # type: ignore[misc]
        assert False, "StreamTermination must be immutable"
    except AttributeError:
        pass
