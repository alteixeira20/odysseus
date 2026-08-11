import inspect
from types import SimpleNamespace

import pytest

from src import agent_loop
from src.agent import api as agent_api
from src.agent.contracts import AgentRunRequest
from src.agent.runner import AgentLoopCompatibilityBackend


@pytest.mark.asyncio
async def test_public_legacy_facade_enters_canonical_agent_api(monkeypatch):
    captured = {}

    async def fake_canonical_stream_agent_loop(**kwargs):
        captured.update(kwargs)
        yield 'data: {"delta": "canonical"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(
        agent_api,
        "stream_agent_loop",
        fake_canonical_stream_agent_loop,
    )

    events = [
        event
        async for event in agent_loop.stream_agent_loop(
            endpoint_url="https://provider.invalid/v1",
            model="model-a",
            messages=[{"role": "user", "content": "hello"}],
            session_id="session-a",
            owner="owner-a",
            workspace="/tmp/workspace",
            relevant_tools={"read_file"},
            shell_enabled=False,
        )
    ]

    assert events == [
        'data: {"delta": "canonical"}\n\n',
        "data: [DONE]\n\n",
    ]
    assert captured["endpoint_url"] == "https://provider.invalid/v1"
    assert captured["model"] == "model-a"
    assert captured["messages"] == [{"role": "user", "content": "hello"}]
    assert captured["session_id"] == "session-a"
    assert captured["owner"] == "owner-a"
    assert captured["workspace"] == "/tmp/workspace"
    assert captured["relevant_tools"] == {"read_file"}
    assert captured["shell_enabled"] is False


def test_public_facade_and_internal_kernel_keep_identical_signature():
    assert inspect.signature(agent_loop.stream_agent_loop) == inspect.signature(
        agent_loop._legacy_stream_agent_kernel
    )


def test_public_facade_source_contains_no_legacy_orchestration_body():
    source = inspect.getsource(agent_loop.stream_agent_loop)

    assert "from src.agent.api import stream_agent_loop as canonical_stream_agent_loop" in source
    assert "async for event in canonical_stream_agent_loop(" in source
    assert "AgentSettingsSnapshot.capture" not in source
    assert "prepare_execution_context_async" not in source
    assert "ProviderAttemptRunner(" not in source
    assert "ToolBatchRunner(" not in source
    assert "ContextBudgetManager" not in source
    assert "_legacy_stream_agent_kernel(" not in source


@pytest.mark.asyncio
async def test_runner_backend_calls_internal_kernel_not_public_facade(monkeypatch):
    calls = []
    context = SimpleNamespace(run_id="run-a")

    async def fake_kernel(**kwargs):
        calls.append(("kernel", kwargs))
        yield 'data: {"delta": "kernel"}\n\n'

    async def forbidden_public(*args, **kwargs):
        raise AssertionError("runner backend must never recurse through public facade")
        yield  # pragma: no cover

    monkeypatch.setattr(agent_loop, "_legacy_stream_agent_kernel", fake_kernel)
    monkeypatch.setattr(agent_loop, "stream_agent_loop", forbidden_public)

    request = AgentRunRequest.from_legacy_arguments(
        endpoint_url="https://provider.invalid/v1",
        model="model-a",
        messages=[{"role": "user", "content": "hello"}],
        session_id="session-a",
        owner="owner-a",
        execution_context=context,
    )
    backend = AgentLoopCompatibilityBackend()

    events = [event async for event in backend.stream(request)]

    assert events == ['data: {"delta": "kernel"}\n\n']
    assert len(calls) == 1
    kind, kwargs = calls[0]
    assert kind == "kernel"
    assert kwargs["execution_context"] is context
    assert kwargs["session_id"] == "session-a"
    assert kwargs["owner"] == "owner-a"


@pytest.mark.asyncio
async def test_public_legacy_call_reaches_default_runner_once(monkeypatch):
    requests = []

    class FakeRunner:
        async def stream(self, request):
            requests.append(request)
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_api, "DEFAULT_AGENT_RUNNER", FakeRunner())

    events = [
        event
        async for event in agent_loop.stream_agent_loop(
            endpoint_url="https://provider.invalid/v1",
            model="model-a",
            messages=[{"role": "user", "content": "hello"}],
            session_id="session-a",
            owner="owner-a",
        )
    ]

    assert events == ["data: [DONE]\n\n"]
    assert len(requests) == 1
    assert requests[0].session_id == "session-a"
    assert requests[0].owner == "owner-a"
