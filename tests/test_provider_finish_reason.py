"""Coverage for the normalized provider finish-reason contract.

See src/agent/providers/finish_reason.py: the runtime previously discarded
the provider's raw finish_reason entirely (src/llm_core.py never read it),
so a text-only round truncated by an output-token limit was
indistinguishable from a deliberate stop. These tests cover the normalizer,
the truncation classifier, and the wire-level "finish" event captured by the
OpenAI-compatible and Ollama-native streaming paths in src/llm_core.py.
"""

import asyncio
import json

from src import llm_core
from src.agent.providers.finish_reason import (
    ProviderFinished,
    ProviderFinishReason,
    classify_truncation,
    normalize_finish_reason,
)
from src.agent.rounds.provider_events import (
    DirectResponseAccumulator,
    ProviderRoundAccumulator,
)
from src.agent.rounds.tool_calls import resolve_round_tool_calls


# ── normalize_finish_reason ─────────────────────────────────────────────

def test_normalize_known_reasons():
    assert normalize_finish_reason("stop") is ProviderFinishReason.STOP
    assert normalize_finish_reason("end_turn") is ProviderFinishReason.STOP
    assert normalize_finish_reason("length") is ProviderFinishReason.LENGTH
    assert normalize_finish_reason("max_tokens") is ProviderFinishReason.LENGTH
    assert normalize_finish_reason("tool_calls") is ProviderFinishReason.TOOL_CALLS
    assert normalize_finish_reason("tool_use") is ProviderFinishReason.TOOL_CALLS
    assert normalize_finish_reason("content_filter") is ProviderFinishReason.CONTENT_FILTER


def test_normalize_missing_or_unknown_is_unknown_not_stop():
    assert normalize_finish_reason(None) is ProviderFinishReason.UNKNOWN
    assert normalize_finish_reason("") is ProviderFinishReason.UNKNOWN
    assert normalize_finish_reason("some_new_provider_code") is ProviderFinishReason.UNKNOWN
    assert normalize_finish_reason(123) is ProviderFinishReason.UNKNOWN  # type: ignore[arg-type]


def test_normalize_is_case_and_whitespace_insensitive():
    assert normalize_finish_reason(" STOP ") is ProviderFinishReason.STOP
    assert normalize_finish_reason("Length") is ProviderFinishReason.LENGTH


# ── classify_truncation ─────────────────────────────────────────────────

def _finished(**overrides):
    base = dict(
        raw_reason="stop",
        normalized_reason=ProviderFinishReason.STOP,
        had_text=True,
        had_native_tool_call_fragment=False,
        had_complete_tool_call=False,
        had_incomplete_native_call=False,
        finish_event_seen=True,
    )
    base.update(overrides)
    return ProviderFinished(**base)


def test_stop_with_complete_answer_is_not_truncated():
    assert classify_truncation(_finished()) is False


def test_length_after_visible_text_is_truncated():
    finished = _finished(raw_reason="length", normalized_reason=ProviderFinishReason.LENGTH)
    assert classify_truncation(finished) is True


def test_incomplete_native_call_is_truncated_even_without_length():
    # e.g. provider omitted finish_reason but a native call's JSON never closed
    finished = _finished(
        raw_reason=None,
        normalized_reason=ProviderFinishReason.UNKNOWN,
        had_native_tool_call_fragment=True,
        had_incomplete_native_call=True,
    )
    assert classify_truncation(finished) is True


def test_unknown_reason_with_clean_text_is_not_truncated():
    # Missing metadata alone must never be treated as truncation — only an
    # explicit LENGTH signal or a demonstrably incomplete tool call is.
    finished = _finished(raw_reason=None, normalized_reason=ProviderFinishReason.UNKNOWN)
    assert classify_truncation(finished) is False


def test_tool_calls_finish_reason_is_not_truncated():
    finished = _finished(raw_reason="tool_calls", normalized_reason=ProviderFinishReason.TOOL_CALLS)
    assert classify_truncation(finished) is False


def test_unclosed_fenced_call_is_truncated_even_without_length():
    finished = _finished(
        raw_reason=None,
        normalized_reason=ProviderFinishReason.UNKNOWN,
        had_unclosed_fenced_call=True,
    )
    assert classify_truncation(finished) is True


# ── Fenced tool-call truncation detection (resolve_round_tool_calls) ────

def test_unclosed_bash_fence_is_flagged_and_not_executed():
    resolved = resolve_round_tool_calls(
        "I'll check that now.\n```bash\nls -la",
        [],
        1,
        is_api_model=False,
    )
    assert resolved.tool_blocks == []
    assert resolved.fenced_call_unclosed is True


def test_closed_bash_fence_is_not_flagged_and_executes_normally():
    resolved = resolve_round_tool_calls(
        "```bash\nls -la\n```",
        [],
        1,
        is_api_model=False,
    )
    assert len(resolved.tool_blocks) == 1
    assert resolved.fenced_call_unclosed is False


def test_unclosed_non_tool_fence_is_not_flagged():
    # An unbalanced ``` used for ordinary formatting (not a recognized tool
    # tag) must never be treated as a truncated tool call.
    resolved = resolve_round_tool_calls(
        "Here's an example config:\n```yaml\nkey: value",
        [],
        1,
        is_api_model=False,
    )
    assert resolved.fenced_call_unclosed is False


def test_unclosed_fence_not_flagged_when_fenced_parsing_is_skipped():
    # API/native-tool-calling models with fenced parsing disabled: a stray
    # fence in prose is display text, not a truncated call — no signal.
    resolved = resolve_round_tool_calls(
        "I'll check that now.\n```bash\nls -la",
        [],
        1,
        is_api_model=True,
        allow_fenced_for_api=False,
    )
    assert resolved.fenced_call_unclosed is False


# ── ProviderRoundAccumulator / DirectResponseAccumulator wiring ─────────

def test_round_accumulator_captures_finish_event():
    state = ProviderRoundAccumulator(requested_model="m", actual_model="m", round_number=1)
    projection = state.consume({"type": "finish", "reason": "length"})

    assert state.finish_event_seen is True
    assert state.raw_finish_reason == "length"
    assert state.normalized_finish_reason is ProviderFinishReason.LENGTH
    assert projection.substantive is False
    assert projection.forward_raw is False
    assert projection.forward_data is None


def test_round_accumulator_finish_event_defaults_before_any_chunk():
    state = ProviderRoundAccumulator(requested_model="m", actual_model="m", round_number=1)
    assert state.finish_event_seen is False
    assert state.raw_finish_reason is None
    assert state.normalized_finish_reason is ProviderFinishReason.UNKNOWN


def test_direct_response_accumulator_captures_finish_event_and_does_not_forward():
    state = DirectResponseAccumulator(requested_model="m", actual_model="m")
    projection = state.consume({"type": "finish", "reason": "stop"})

    assert state.finish_event_seen is True
    assert state.normalized_finish_reason is ProviderFinishReason.STOP
    # Must not leak the internal wire event to SSE consumers.
    assert projection.forward_raw is False
    assert projection.forward_data is None


# ── Wire-level capture in src/llm_core.py (OpenAI-compatible + Ollama) ──

class _FakeResp:
    def __init__(self, lines):
        self._lines = lines
        self.status_code = 200

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResp(self._lines)

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._lines = lines

    def stream(self, method, url, **kw):
        return _FakeStreamCtx(self._lines)


def _drive(monkeypatch, lines, url, model="test-model"):
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeClient(lines))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_mark_host_dead", lambda *a, **k: False, raising=False)

    async def run():
        return [
            chunk
            async for chunk in llm_core.stream_llm(
                url, model, [{"role": "user", "content": "hi"}],
                headers={"Authorization": "Bearer k"},
            )
        ]

    return asyncio.run(run())


def _finish_events(chunks):
    events = []
    for chunk in chunks:
        for line in chunk.split("\n"):
            line = line.strip()
            if line.startswith("data: ") and line[6:] != "[DONE]":
                try:
                    j = json.loads(line[6:])
                except ValueError:
                    continue
                if j.get("type") == "finish":
                    events.append(j)
    return events


_OPENAI_COMPAT_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


def test_openai_compatible_captures_length_finish_reason(monkeypatch):
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Now the core dispatch entry point:"}}]}),
        'data: ' + json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]}),
        'data: [DONE]',
    ]
    events = _finish_events(_drive(monkeypatch, lines, _OPENAI_COMPAT_URL))
    assert events == [
        {"type": "finish", "reason": "length", "protocol_terminal_seen": True}
    ]


def test_openai_compatible_emits_unknown_finish_when_provider_omits_it(monkeypatch):
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Hello"}}]}),
        'data: [DONE]',
    ]
    events = _finish_events(_drive(monkeypatch, lines, _OPENAI_COMPAT_URL))
    # The provider never sent a finish_reason, but it DID send the explicit
    # [DONE] sentinel — a legitimate protocol completion, not a transport
    # interruption.
    assert events == [
        {"type": "finish", "reason": None, "protocol_terminal_seen": True}
    ]


def test_openai_compatible_emits_unknown_finish_when_stream_ends_without_done(monkeypatch):
    # Simulates a disconnect: no [DONE] sentinel ever arrives. This is the
    # raw-transport-EOF case (see src/agent/providers/termination.py) —
    # protocol_terminal_seen must be False so the agent runtime does not
    # silently accept the partial answer as deliberate.
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Hello"}}]}),
    ]
    events = _finish_events(_drive(monkeypatch, lines, _OPENAI_COMPAT_URL))
    assert events == [
        {"type": "finish", "reason": None, "protocol_terminal_seen": False}
    ]


def test_openai_compatible_finish_event_emitted_only_once(monkeypatch):
    lines = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]}),
        'data: ' + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        'data: [DONE]',
    ]
    events = _finish_events(_drive(monkeypatch, lines, _OPENAI_COMPAT_URL))
    assert len(events) == 1
    assert events[0]["reason"] == "stop"


def test_openai_compatible_finish_ordered_after_same_chunk_content(monkeypatch):
    # Some providers/proxies put finish_reason on the SAME chunk as the
    # final content delta, rather than on a separate trailing chunk. The
    # "finish" event must never be yielded ahead of that chunk's own
    # content — downstream code must see all of a chunk's deltas before
    # the terminal metadata that closes it out.
    lines = [
        'data: ' + json.dumps({
            "choices": [{
                "delta": {"content": "final piece"},
                "finish_reason": "stop",
            }]
        }),
        'data: [DONE]',
    ]
    chunks = _drive(monkeypatch, lines, _OPENAI_COMPAT_URL)

    content_idx = next(
        i for i, c in enumerate(chunks) if '"delta": "final piece"' in c
    )
    finish_idx = next(
        i for i, c in enumerate(chunks) if '"type": "finish"' in c
    )
    assert content_idx < finish_idx


def test_openai_compatible_finish_ordered_after_same_chunk_tool_call(monkeypatch):
    # A chunk carrying both the final native tool-call fragment and
    # finish_reason="tool_calls" must have its tool-call state fully
    # accumulated before the terminal event is emitted.
    lines = [
        'data: ' + json.dumps({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"path": "a"}'},
                    }]
                },
                "finish_reason": "tool_calls",
            }]
        }),
        'data: [DONE]',
    ]
    chunks = _drive(monkeypatch, lines, _OPENAI_COMPAT_URL)

    tool_calls_idx = next(
        i for i, c in enumerate(chunks) if '"type": "tool_calls"' in c
    )
    finish_idx = next(
        i for i, c in enumerate(chunks) if '"type": "finish"' in c
    )
    assert tool_calls_idx < finish_idx


def test_ollama_native_captures_done_reason(monkeypatch):
    lines = [
        json.dumps({"message": {"content": "Hi"}, "done": False}),
        json.dumps({"message": {"content": ""}, "done": True, "done_reason": "length"}),
    ]
    events = _finish_events(_drive(monkeypatch, lines, "http://localhost:11434"))
    assert events == [
        {"type": "finish", "reason": "length", "protocol_terminal_seen": True}
    ]


def test_ollama_native_emits_unknown_finish_when_stream_ends_without_done(monkeypatch):
    # Raw transport EOF: the connection drops before Ollama ever sends
    # "done": true. Previously this branch emitted no finish event at all,
    # making the interruption invisible to the rest of the runtime.
    lines = [
        json.dumps({"message": {"content": "Hi"}, "done": False}),
    ]
    events = _finish_events(_drive(monkeypatch, lines, "http://localhost:11434"))
    assert events == [
        {"type": "finish", "reason": None, "protocol_terminal_seen": False}
    ]


# ── Wire-level capture: Anthropic-native + ChatGPT Subscription ─────────
# These two branches previously never emitted a "finish" event at all
# (only OpenAI-compatible and Ollama-native did); see next-slices note in
# specs/agent-runtime-v2-progress.md.

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_CHATGPT_SUBSCRIPTION_URL = "https://chatgpt.com/backend-api/codex"


def test_anthropic_native_captures_max_tokens_stop_reason(monkeypatch):
    lines = [
        'data: ' + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
        'data: ' + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        'data: ' + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
        'data: ' + json.dumps({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 5}}),
        'data: ' + json.dumps({"type": "message_stop"}),
    ]
    chunks = _drive(monkeypatch, lines, _ANTHROPIC_URL)
    events = _finish_events(chunks)
    assert events == [
        {"type": "finish", "reason": "max_tokens", "protocol_terminal_seen": True}
    ]
    assert normalize_finish_reason(events[0]["reason"]) is ProviderFinishReason.LENGTH

    content_idx = next(i for i, c in enumerate(chunks) if '"delta": "Hi"' in c)
    finish_idx = next(i for i, c in enumerate(chunks) if '"type": "finish"' in c)
    assert content_idx < finish_idx


def test_anthropic_native_captures_clean_end_turn(monkeypatch):
    lines = [
        'data: ' + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        'data: ' + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
        'data: ' + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}),
        'data: ' + json.dumps({"type": "message_stop"}),
    ]
    events = _finish_events(_drive(monkeypatch, lines, _ANTHROPIC_URL))
    assert events == [
        {"type": "finish", "reason": "end_turn", "protocol_terminal_seen": True}
    ]
    assert normalize_finish_reason(events[0]["reason"]) is ProviderFinishReason.STOP


def test_anthropic_native_emits_unknown_finish_when_stream_ends_without_message_stop(monkeypatch):
    lines = [
        'data: ' + json.dumps({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}),
        'data: ' + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}}),
    ]
    events = _finish_events(_drive(monkeypatch, lines, _ANTHROPIC_URL))
    assert events == [
        {"type": "finish", "reason": None, "protocol_terminal_seen": False}
    ]


def test_chatgpt_subscription_captures_incomplete_max_output_tokens(monkeypatch):
    lines = [
        'data: ' + json.dumps({"type": "response.output_text.delta", "delta": "Hi"}),
        'data: ' + json.dumps({
            "type": "response.completed",
            "response": {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        }),
    ]
    chunks = _drive(monkeypatch, lines, _CHATGPT_SUBSCRIPTION_URL)
    events = _finish_events(chunks)
    assert events == [
        {
            "type": "finish",
            "reason": "max_output_tokens",
            "protocol_terminal_seen": True,
        }
    ]
    assert normalize_finish_reason(events[0]["reason"]) is ProviderFinishReason.LENGTH

    content_idx = next(i for i, c in enumerate(chunks) if '"delta": "Hi"' in c)
    finish_idx = next(i for i, c in enumerate(chunks) if '"type": "finish"' in c)
    assert content_idx < finish_idx


def test_chatgpt_subscription_captures_clean_completed(monkeypatch):
    lines = [
        'data: ' + json.dumps({"type": "response.output_text.delta", "delta": "Hi"}),
        'data: ' + json.dumps({
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"input_tokens": 1, "output_tokens": 2}},
        }),
    ]
    events = _finish_events(_drive(monkeypatch, lines, _CHATGPT_SUBSCRIPTION_URL))
    assert events == [
        {"type": "finish", "reason": "stop", "protocol_terminal_seen": True}
    ]
    assert normalize_finish_reason(events[0]["reason"]) is ProviderFinishReason.STOP


def test_chatgpt_subscription_emits_unknown_finish_when_stream_ends_without_completed(monkeypatch):
    lines = [
        'data: ' + json.dumps({"type": "response.output_text.delta", "delta": "Hi"}),
    ]
    events = _finish_events(_drive(monkeypatch, lines, _CHATGPT_SUBSCRIPTION_URL))
    assert events == [
        {"type": "finish", "reason": None, "protocol_terminal_seen": False}
    ]
