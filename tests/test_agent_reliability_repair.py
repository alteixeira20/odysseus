import os
import json
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import src.agent_tools.subprocess_tools as subprocess_tools
from src.agent_tools.subprocess_tools import _ensure_tmux_session, _tmux_session_name
from src.llm_core import apply_kimi_code_headers_async, _stream_llm_inner
from src.tool_execution import vet_workspace
from src.agent_loop import stream_agent_loop, _is_transient_error


def test_workspace_canonicalization(tmp_path):
    d = tmp_path / "test_ws"
    d.mkdir()
    res = vet_workspace(str(d))
    assert res == str(d.resolve())
    assert os.path.isabs(res)


@pytest.mark.asyncio
async def test_existing_workspace_keyed_tmux_session_preserves_cd_state(monkeypatch):
    calls = []

    async def mock_run_exec(*args, timeout=5):
        calls.append(args)
        return ("", "", 0)

    async def mock_has_session(name):
        return True

    monkeypatch.setattr(subprocess_tools, "_run_exec", mock_run_exec)
    monkeypatch.setattr(subprocess_tools, "_tmux_has_session", mock_has_session)

    await _ensure_tmux_session("test_sess", cwd="/tmp/my_target_workspace", env=None)
    cd_issued = any("send-keys" in c and "cd '/tmp/my_target_workspace'" in c for c in calls)
    assert not cd_issued, f"same-workspace reuse must preserve shell cwd: {calls}"
    assert _tmux_session_name("chat", "/tmp/one") != _tmux_session_name("chat", "/tmp/two")


@pytest.mark.asyncio
async def test_kimi_code_header_selection_performs_no_preflight_probe(monkeypatch):
    probe_calls = []

    async def mock_get(url, headers=None, timeout=None):
        probe_calls.append(timeout)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"models": []}'
        return mock_resp

    mock_client = AsyncMock()
    mock_client.get = mock_get

    headers = await apply_kimi_code_headers_async(mock_client, {}, "https://api.kimi.com/coding/v1/chat/completions")
    assert headers is not None
    assert probe_calls == []


@pytest.mark.asyncio
async def test_kimi_stream_reasoning_deltas_extracted(monkeypatch):
    class DummyResponse:
        status_code = 200
        async def aiter_lines(self):
            chunks = [
                'data: {"choices": [{"delta": {"reasoning_text": "Thinking step 1"}}]}',
                'data: {"choices": [{"delta": {"thought": " thinking step 2"}}]}',
                'data: {"choices": [{"delta": {"content": "Final answer"}}]}',
                'data: [DONE]'
            ]
            for c in chunks:
                yield c

    class DummyClient:
        def stream(self, *args, **kwargs):
            class AsyncContextManager:
                async def __aenter__(self):
                    return DummyResponse()
                async def __aexit__(self, exc_type, exc, tb):
                    pass
            return AsyncContextManager()

    monkeypatch.setattr("src.llm_core._get_http_client", lambda: DummyClient())

    yielded_events = []
    async for event in _stream_llm_inner("https://api.openai.com/v1/chat/completions", "gpt-4o", []):
        yielded_events.append(event)

    thinking_events = []
    content_events = []
    for e in yielded_events:
        if e.startswith("data: ") and not e.startswith("data: [DONE]"):
            data = json.loads(e[6:])
            if data.get("thinking"):
                thinking_events.append(data.get("delta"))
            elif data.get("delta"):
                content_events.append(data.get("delta"))

    assert "Thinking step 1" in thinking_events
    assert " thinking step 2" in thinking_events
    assert "Final answer" in content_events


def test_is_transient_error_detection():
    sse_502 = 'event: error\ndata: {"status": 502, "text": "502 Bad Gateway"}\n\n'
    sse_503 = 'event: error\ndata: {"status": 503, "text": "Service Unavailable"}\n\n'
    sse_400 = 'event: error\ndata: {"status": 400, "text": "Bad Request"}\n\n'
    assert _is_transient_error(sse_502) is True
    assert _is_transient_error(sse_503) is True
    assert _is_transient_error(sse_400) is False
    assert _is_transient_error("data: hello\n\n") is False


@pytest.mark.asyncio
async def test_transient_http_502_retries_and_recovers(monkeypatch):
    call_count = 0

    async def mock_stream_llm_with_fallback(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield 'event: error\ndata: {"status": 502, "text": "502 Bad Gateway"}\n\n'
        else:
            yield 'data: {"delta": "Hello recovered!"}\n\n'
            yield 'data: [DONE]\n\n'

    monkeypatch.setattr("src.agent_loop.stream_llm_with_fallback", mock_stream_llm_with_fallback)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    events = []
    async for chunk in stream_agent_loop(
        endpoint_url="http://dummy",
        model="test-model",
        messages=[{"role": "user", "content": "Please inspect files and run pytest in workspace"}],
        workspace="/tmp"
    ):
        events.append(chunk)

    assert call_count == 2
    retry_events = [e for e in events if "Retrying provider request" in e]
    assert len(retry_events) == 1
    content_events = [e for e in events if "Hello recovered!" in e]
    assert len(content_events) == 1


@pytest.mark.asyncio
async def test_transient_error_exceeds_max_retries_emits_error_then_done(monkeypatch):
    call_count = 0

    async def mock_stream_llm_with_fallback(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        yield 'event: error\ndata: {"status": 502, "text": "502 Bad Gateway"}\n\n'

    monkeypatch.setattr("src.agent_loop.stream_llm_with_fallback", mock_stream_llm_with_fallback)
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    events = []
    async for chunk in stream_agent_loop(
        endpoint_url="http://dummy",
        model="test-model",
        messages=[{"role": "user", "content": "Please inspect files and run pytest in workspace"}],
        workspace="/tmp"
    ):
        events.append(chunk)

    assert call_count == 3
    assert sum("event: error" in e for e in events) == 1
    assert sum("data: [DONE]" in e for e in events) == 1
    assert any('"state": "error"' in e and '"terminal": true' in e for e in events)


@pytest.mark.asyncio
async def test_initial_agent_status_emitted():
    async def mock_stream_llm_with_fallback(*args, **kwargs):
        yield 'data: {"delta": "Hi"}\n\n'
        yield 'data: [DONE]\n\n'

    with patch("src.agent_loop.stream_llm_with_fallback", mock_stream_llm_with_fallback):
        events = []
        async for chunk in stream_agent_loop(
            endpoint_url="http://dummy",
            model="test-model",
            messages=[{"role": "user", "content": "Please inspect files and run pytest"}],
        ):
            events.append(chunk)

    assert len(events) > 0
    first_event = json.loads(events[0].replace("data: ", "").strip())
    assert first_event.get("version") == 2
    assert first_event.get("type") == "run_state"
    assert first_event.get("payload") == {
        "state": "preparing",
        "reason": "request_prepared",
    }


@pytest.mark.asyncio
async def test_client_disconnect_cancels_without_retry(monkeypatch):
    call_count = 0

    async def mock_stream_llm_with_fallback(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if False:
            yield
        raise asyncio.CancelledError()

    monkeypatch.setattr("src.agent_loop.stream_llm_with_fallback", mock_stream_llm_with_fallback)
    sleep_mock = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", sleep_mock)

    with pytest.raises(asyncio.CancelledError):
        gen = stream_agent_loop(
            endpoint_url="http://dummy",
            model="test-model",
            messages=[{"role": "user", "content": "Please inspect files and run pytest"}],
        )
        while True:
            await gen.__anext__()

    assert call_count == 1
    sleep_mock.assert_not_called()
