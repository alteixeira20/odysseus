from dataclasses import dataclass

import pytest

from src.agent.contracts import AgentRunRequest
from src.agent.runner import AgentLoopCompatibilityBackend, AgentRunner
from src.agent.tools.bootstrap import TOOL_REGISTRY


@dataclass
class _Context:
    run_id: str = "run-test"


class _Backend:
    def __init__(self):
        self.requests = []

    def stream(self, request):
        self.requests.append(request)

        async def generate():
            yield 'data: {"delta": "ok"}\n\n'
            yield "data: [DONE]\n\n"

        return generate()


def _request(**kwargs):
    values = {
        "endpoint_url": "https://provider.invalid/v1",
        "model": "model-a",
        "messages": [{"role": "user", "content": "do the thing"}],
        "session_id": "session-a",
        "owner": "owner-a",
        "max_rounds": 7,
        "max_tool_calls": 11,
        "relevant_tools": {"read_file"},
        "forced_tools": {"read_file"},
        "shell_enabled": False,
    }
    values.update(kwargs)
    return AgentRunRequest.from_legacy_arguments(**values)


def test_compatibility_backend_is_only_typed_to_legacy_projection():
    context = _Context()
    request = _request(execution_context=context)

    arguments = AgentLoopCompatibilityBackend.arguments(request)

    assert arguments["endpoint_url"] == request.endpoint_url
    assert arguments["model"] == request.model
    assert arguments["messages"] is request.messages
    assert arguments["max_rounds"] == 7
    assert arguments["max_tool_calls"] == 11
    assert arguments["relevant_tools"] == {"read_file"}
    assert arguments["forced_tools"] == {"read_file"}
    assert arguments["shell_enabled"] is False
    assert arguments["execution_context"] is context


@pytest.mark.asyncio
async def test_runner_preserves_server_prepared_execution_context():
    context = _Context()
    request = _request(execution_context=context)
    prepare_calls = []

    async def forbidden_prepare(**kwargs):
        prepare_calls.append(kwargs)
        raise AssertionError("already-prepared request must not be prepared twice")

    runner = AgentRunner(prepare_execution_context=forbidden_prepare)
    prepared = await runner.prepare(request)

    assert prepared.request is request
    assert prepared.request.execution_context is context
    assert prepared.prepared_here is False
    assert prepared.authority_reason == "provided"
    assert prepare_calls == []


@pytest.mark.asyncio
async def test_runner_prepares_missing_execution_context_with_canonical_catalog(monkeypatch):
    captured = {}
    context = _Context()

    async def fake_prepare(**kwargs):
        captured.update(kwargs)
        return context, "requested"

    monkeypatch.setattr("src.agent.runner.get_setting", lambda key, default=None: default)
    runner = AgentRunner(prepare_execution_context=fake_prepare)
    request = _request(execution_context=None)

    prepared = await runner.prepare(request)

    assert prepared.prepared_here is True
    assert prepared.authority_reason == "requested"
    assert prepared.request is not request
    assert prepared.request.execution_context is context
    assert request.execution_context is None
    assert captured["owner_id"] == "owner-a"
    assert captured["session_id"] == "session-a"
    assert captured["tool_catalog_revision"] == TOOL_REGISTRY.revision
    assert captured["budgets"].max_rounds == 7
    assert captured["budgets"].max_tool_calls == 11
    assert "read_file" not in captured["disabled_tools"]


@pytest.mark.asyncio
async def test_runner_owns_prepare_durability_then_backend_order(monkeypatch):
    order = []
    context = _Context()
    backend = _Backend()

    async def fake_prepare(**kwargs):
        order.append("prepare")
        return context, "requested"

    async def fake_durable(request, factory):
        order.append("durable")
        assert request.execution_context is context
        stream = factory()
        async for event in stream:
            yield event

    monkeypatch.setattr("src.agent.runner.get_setting", lambda key, default=None: default)
    runner = AgentRunner(
        backend=backend,
        durable_stream=fake_durable,
        prepare_execution_context=fake_prepare,
    )

    events = [event async for event in runner.stream(_request(execution_context=None))]

    assert order == ["prepare", "durable"]
    assert events == ['data: {"delta": "ok"}\n\n', "data: [DONE]\n\n"]
    assert len(backend.requests) == 1
    assert backend.requests[0].execution_context is context


@pytest.mark.asyncio
async def test_runner_plan_mode_contributes_fail_closed_disabled_tools(monkeypatch):
    captured = {}
    context = _Context()

    async def fake_prepare(**kwargs):
        captured.update(kwargs)
        return context, "requested"

    monkeypatch.setattr("src.agent.runner.get_setting", lambda key, default=None: default)
    monkeypatch.setattr(
        "src.agent.runner.plan_mode_disabled_tools",
        lambda: {"bash", "write_file"},
    )
    runner = AgentRunner(prepare_execution_context=fake_prepare)
    request = _request(plan_mode=True, execution_context=None)

    await runner.prepare(request)

    assert "bash" in captured["disabled_tools"]
    assert "write_file" in captured["disabled_tools"]
