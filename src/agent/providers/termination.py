"""Typed classification of how one provider stream round actually ended.

Root cause context: ``src/llm_core.py``'s streaming branches synthesize a
``{"type": "finish", "reason": null}`` event (or, for the Ollama branch,
nothing at all) whenever the underlying HTTP iterator runs out of lines
*without* the provider ever sending its own terminal signal (an explicit
``[DONE]`` sentinel, Ollama's ``"done": true``, Anthropic's
``message_stop``, or the Responses API's ``response.completed``). That
"stream just stopped" state was, until now, indistinguishable from a
provider that legitimately closes the connection after an explicit
protocol-terminal signal — both looked like "the generator returned". A
raw transport error (read timeout, connection reset, malformed chunked
body) arriving *after* partial content had already streamed was worse: it
was treated as an unconditional fatal run-ending error, discarding any
chance of the existing truncation-continuation machinery
(``src/agent/supervision/continuation.py``) safely resuming the answer.

This module gives the rest of the runtime a typed vocabulary for that
distinction. ``src/llm_core.py`` tags each finish/error event with the raw
ingredients (``protocol_terminal_seen``, ``error_kind``); ``src/agent_loop.py``
combines them with round-local knowledge (was a complete tool call
resolved this round?) into a :class:`StreamTermination` and attaches it to
``ProviderFinished`` so ``classify_truncation`` can treat a recoverable
interruption exactly like a provider-reported truncation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class StreamTerminationKind(str, Enum):
    PROVIDER_TERMINAL = "provider_terminal"
    PROTOCOL_DONE = "protocol_done"
    TRANSPORT_EOF = "transport_eof"
    TRANSPORT_ERROR = "transport_error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PARSE_ERROR = "parse_error"
    UNKNOWN = "unknown"


# Error-kind tags emitted by src/llm_core.py's `event: error` payloads
# (field "error_kind") for the httpx exception branches that unambiguously
# indicate a transport-layer failure rather than a provider-reported error.
_ERROR_KIND_TO_TERMINATION = {
    "connect": StreamTerminationKind.TRANSPORT_ERROR,
    "read_timeout": StreamTerminationKind.TIMEOUT,
    "network": StreamTerminationKind.TRANSPORT_ERROR,
    "protocol": StreamTerminationKind.TRANSPORT_ERROR,
    "unknown_exception": StreamTerminationKind.TRANSPORT_ERROR,
}

# Kinds a caller may safely retry/continue for: no provider-terminal signal
# was ever received, so the round's answer is not known to be finished.
RECOVERABLE_KINDS = frozenset(
    {
        StreamTerminationKind.TRANSPORT_EOF,
        StreamTerminationKind.TRANSPORT_ERROR,
        StreamTerminationKind.TIMEOUT,
    }
)


@dataclass(frozen=True)
class StreamTermination:
    """Terminal transport/protocol state for one provider round attempt."""

    kind: StreamTerminationKind
    provider_finish_seen: bool
    protocol_done_seen: bool
    transport_closed_cleanly: bool
    had_visible_text: bool
    had_reasoning: bool
    had_tool_call_fragment: bool
    had_complete_tool_call: bool
    error_type: Optional[str] = None

    @property
    def recoverable(self) -> bool:
        """True when this round may safely be retried/continued.

        Never true for a round that already produced a complete tool call
        (that call must be executed or discarded by the normal tool-call
        path, not silently re-requested) nor for cancellation/parse errors.
        """
        return self.kind in RECOVERABLE_KINDS and not self.had_complete_tool_call


def error_kind_to_termination_kind(
    error_kind: Optional[str],
) -> Optional[StreamTerminationKind]:
    """Map the `error_kind` tag on an `event: error` chunk to a termination kind."""

    if not error_kind:
        return None
    return _ERROR_KIND_TO_TERMINATION.get(error_kind)


def classify_stream_termination(
    *,
    provider_finish_seen: bool,
    protocol_done_seen: bool,
    error_kind: Optional[str] = None,
    deadline_exceeded: bool = False,
    cancelled: bool = False,
    parse_error: bool = False,
    normalized_reason_is_known: bool = False,
    had_visible_text: bool = False,
    had_reasoning: bool = False,
    had_tool_call_fragment: bool = False,
    had_complete_tool_call: bool = False,
) -> StreamTermination:
    """Classify how a round ended from its raw transport/protocol ingredients.

    Precedence (most specific/dangerous first): cancellation, then a
    transport-tagged error, then an explicit deadline cutoff, then parser
    failure, then a genuine provider terminal signal, then explicit
    protocol completion, then — only if the round produced no terminal
    signal of any kind — a raw transport EOF.
    """

    if cancelled:
        kind = StreamTerminationKind.CANCELLED
    elif error_kind and error_kind_to_termination_kind(error_kind) is not None:
        kind = error_kind_to_termination_kind(error_kind)
    elif deadline_exceeded:
        kind = StreamTerminationKind.TIMEOUT
    elif parse_error:
        kind = StreamTerminationKind.PARSE_ERROR
    elif normalized_reason_is_known and provider_finish_seen:
        kind = StreamTerminationKind.PROVIDER_TERMINAL
    elif protocol_done_seen:
        kind = StreamTerminationKind.PROTOCOL_DONE
    elif not provider_finish_seen:
        # Iterator ended (or errored pre-content) without ever reporting a
        # finish event at all — e.g. the Ollama branch's fallback path, or
        # no bytes were received before the transport gave up.
        kind = StreamTerminationKind.TRANSPORT_EOF
    else:
        # A finish event arrived (possibly synthesized with reason=None)
        # but no explicit protocol-terminal signal was ever seen on the
        # wire: the generator's own loop simply ran out of lines.
        kind = StreamTerminationKind.TRANSPORT_EOF

    return StreamTermination(
        kind=kind,
        provider_finish_seen=provider_finish_seen,
        protocol_done_seen=protocol_done_seen,
        transport_closed_cleanly=not bool(error_kind),
        had_visible_text=had_visible_text,
        had_reasoning=had_reasoning,
        had_tool_call_fragment=had_tool_call_fragment,
        had_complete_tool_call=had_complete_tool_call,
        error_type=error_kind,
    )
