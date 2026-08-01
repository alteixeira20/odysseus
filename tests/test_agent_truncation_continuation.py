"""End-to-end coverage for safe automatic continuation after a truncated,
tool-free round (src/agent_loop.py, `if not tool_blocks:` branch).

Root cause: a Kimi-K3 run stopped twice mid-task right after announcement
text with no following tool call. src/llm_core.py never read the provider's
finish_reason, so the round loop's ``if not tool_blocks: ... break # no
tools — done`` treated an output-limit cutoff exactly like a deliberate
final answer. These tests drive stream_agent_loop with a fake provider
(monkeypatching stream_llm_with_fallback, the same seam test_ask_user_
persistence.py uses) to prove the loop now tells the two cases apart.
"""

import asyncio
import json

import src.agent_loop as agent_loop


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def _events(chunks):
    events = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            events.append(json.loads(chunk[6:]))
    return events


def _patch_common(monkeypatch):
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *a, **k: 10, raising=False)


def test_length_truncation_with_no_tool_call_resumes_instead_of_finishing(monkeypatch):
    calls = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield f'data: {json.dumps({"delta": "Now the core dispatch entry point:"})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "reason": "length"})}\n\n'
            yield "data: [DONE]\n\n"
        else:
            yield f'data: {json.dumps({"delta": " def dispatch(): return route(request)"})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "reason": "stop"})}\n\n'
            yield "data: [DONE]\n\n"

    _patch_common(monkeypatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)

    chunks = _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "Refactor the dispatcher."}],
            relevant_tools={"bash"},
            _is_teacher_run=True,
        )
    )
    events = _events(chunks)

    # The provider was invoked twice: the truncated attempt, then the
    # continuation — proving the turn did not silently stop after round 1.
    assert calls["n"] == 2

    continuations = [e for e in events if e.get("type") == "truncation_continuation"]
    assert len(continuations) == 1
    assert continuations[0]["reason"] == "length"
    assert continuations[0]["attempt"] == 1

    full_text = "".join(e.get("delta", "") for e in events if "delta" in e and not e.get("thinking"))
    assert "Now the core dispatch entry point:" in full_text
    assert "def dispatch(): return route(request)" in full_text


def test_stop_with_no_tool_call_finishes_normally_without_continuation(monkeypatch):
    calls = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        yield f'data: {json.dumps({"delta": "Here is the answer."})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "reason": "stop"})}\n\n'
        yield "data: [DONE]\n\n"

    _patch_common(monkeypatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)

    chunks = _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "Say something."}],
            relevant_tools={"bash"},
            _is_teacher_run=True,
        )
    )
    events = _events(chunks)

    assert calls["n"] == 1
    assert not [e for e in events if e.get("type") == "truncation_continuation"]


def test_transport_interruption_with_partial_text_resumes_instead_of_erroring(monkeypatch):
    # A read timeout / connection reset arriving mid-stream, after partial
    # content already flowed, must not fatally kill the whole run — it
    # should hand off to the same safe continuation path as an output-limit
    # truncation (src/agent/providers/termination.py).
    calls = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield f'data: {json.dumps({"delta": "Partial answer before the drop"})}\n\n'
            yield (
                "event: error\ndata: "
                + json.dumps(
                    {
                        "status": 502,
                        "error": "Network error",
                        "error_kind": "network",
                    }
                )
                + "\n\n"
            )
        else:
            yield f'data: {json.dumps({"delta": " and the rest after resuming."})}\n\n'
            yield f'data: {json.dumps({"type": "finish", "reason": "stop", "protocol_terminal_seen": True})}\n\n'
            yield "data: [DONE]\n\n"

    _patch_common(monkeypatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)

    chunks = _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "Tell me something long."}],
            relevant_tools={"bash"},
            _is_teacher_run=True,
        )
    )
    events = _events(chunks)

    assert calls["n"] == 2
    # Must never reach the fatal run-terminal-error path.
    error_terminals = [
        e for e in events if e.get("type") == "run_state" and e.get("state") == "error"
    ]
    assert error_terminals == []

    continuations = [e for e in events if e.get("type") == "truncation_continuation"]
    assert len(continuations) == 1
    assert continuations[0]["cause"] == "transport_interruption"
    assert continuations[0]["termination_kind"] == "transport_error"

    full_text = "".join(e.get("delta", "") for e in events if "delta" in e and not e.get("thinking"))
    assert "Partial answer before the drop" in full_text
    assert "and the rest after resuming." in full_text


def test_hard_provider_error_after_content_still_ends_run_with_error(monkeypatch):
    # Regression anchor: a genuinely fatal, non-transport error (e.g. a 400
    # from a malformed request) arriving after content must still end the
    # run — only transport-shaped interruptions get the safe-continuation
    # treatment.
    calls = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        yield f'data: {json.dumps({"delta": "Partial answer"})}\n\n'
        yield (
            "event: error\ndata: "
            + json.dumps({"status": 400, "text": "Bad request"})
            + "\n\n"
        )

    _patch_common(monkeypatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)

    chunks = _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "Do something."}],
            relevant_tools={"bash"},
            _is_teacher_run=True,
        )
    )
    events = _events(chunks)

    assert calls["n"] == 1
    assert not [e for e in events if e.get("type") == "truncation_continuation"]
    error_terminals = [
        e for e in events if e.get("type") == "run_state" and e.get("state") == "error"
    ]
    assert len(error_terminals) == 1


def test_transport_interruption_before_any_text_uses_existing_retry_policy(monkeypatch):
    # No content ever streamed — this is the existing pre-content transient-
    # error retry path (unchanged by this pass), not the new substantive-
    # interruption path. Exhausting retries still surfaces a terminal error
    # rather than fabricating a fake success.
    calls = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        yield (
            "event: error\ndata: "
            + json.dumps(
                {"status": 502, "text": "bad gateway", "error_kind": "network"}
            )
            + "\n\n"
        )

    _patch_common(monkeypatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)

    chunks = _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "Hello."}],
            relevant_tools={"bash"},
            _is_teacher_run=True,
        )
    )
    events = _events(chunks)

    assert calls["n"] >= 2  # retried at least once before giving up
    error_terminals = [
        e for e in events if e.get("type") == "run_state" and e.get("state") == "error"
    ]
    assert len(error_terminals) == 1


def test_truncation_continuations_are_bounded_per_run(monkeypatch):
    # A model that keeps hitting the output limit every round must not loop
    # forever: the continuation cap must eventually let existing supervision
    # (here: no-tools "done" fallback) take over.
    calls = {"n": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        calls["n"] += 1
        partial_text = "partial " + str(calls["n"])
        yield f'data: {json.dumps({"delta": partial_text})}\n\n'
        yield f'data: {json.dumps({"type": "finish", "reason": "length"})}\n\n'
        yield "data: [DONE]\n\n"

    _patch_common(monkeypatch)
    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)

    chunks = _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "Keep going forever."}],
            relevant_tools={"bash"},
            _is_teacher_run=True,
        )
    )
    events = _events(chunks)

    continuations = [e for e in events if e.get("type") == "truncation_continuation"]
    # Bounded by agent_loop._MAX_TRUNCATION_CONTINUATIONS (4): the 5th
    # truncated round must terminate explicitly as resumable incomplete.
    assert len(continuations) == 4
    assert calls["n"] == 5
    terminal = next(
        event for event in events
        if event.get("type") == "run_state" and event.get("terminal")
    )
    assert terminal == {
        "type": "run_state",
        "state": "incomplete",
        "terminal": True,
        "reason": "truncation_retries_exhausted",
        "resumable": True,
    }
