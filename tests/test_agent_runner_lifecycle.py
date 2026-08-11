from types import SimpleNamespace

import pytest

from src.agent.contracts import AgentRunRequest
from src.agent.runner import AgentRunner


class _Backend:
    def __init__(self, order):
        self.order = order

    def stream(self, request):
        self.order.append("backend")

        async def generate():
            yield "data: [DONE]\n\n"

        return generate()


class _Lifecycle:
    def __init__(self, order):
        self.order = order

    def stream(self, request, backend_factory):
        self.order.append("lifecycle")

        async def generate():
            stream = backend_factory()
            async for wire in stream:
                yield wire

        return generate()


def _prepared_request():
    context = SimpleNamespace(run_id="run-a")
    return AgentRunRequest.from_legacy_arguments(
        endpoint_url="https://provider.invalid/v1",
        model="model-a",
        messages=[{"role": "user", "content": "hello"}],
        execution_context=context,
    )


@pytest.mark.asyncio
async def test_default_runner_path_creates_one_lifecycle_per_run():
    order = []
    instances = []

    def lifecycle_factory():
        lifecycle = _Lifecycle(order)
        instances.append(lifecycle)
        return lifecycle

    runner = AgentRunner(
        backend=_Backend(order),
        lifecycle_factory=lifecycle_factory,
    )

    events = [event async for event in runner.stream(_prepared_request())]

    assert events == ["data: [DONE]\n\n"]
    assert order == ["lifecycle", "backend"]
    assert len(instances) == 1


@pytest.mark.asyncio
async def test_transitional_durable_stream_injection_does_not_create_lifecycle():
    created = []

    def forbidden_lifecycle():
        created.append(True)
        raise AssertionError("durable_stream injection must remain isolated")

    async def injected(request, backend_factory):
        stream = backend_factory()
        async for wire in stream:
            yield wire

    runner = AgentRunner(
        backend=_Backend([]),
        lifecycle_factory=forbidden_lifecycle,
        durable_stream=injected,
    )

    events = [event async for event in runner.stream(_prepared_request())]

    assert events == ["data: [DONE]\n\n"]
    assert created == []
