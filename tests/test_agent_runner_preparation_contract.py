import pytest

from src.agent.contracts import AgentRunRequest
from src.agent.runner import AgentRunner


class _PreparedContext:
    run_id = "prepared-run"


@pytest.mark.asyncio
async def test_prepared_context_is_not_reconstructed_or_rebound():
    context = _PreparedContext()
    request = AgentRunRequest.from_legacy_arguments(
        endpoint_url="https://provider.invalid/v1",
        model="model-a",
        messages=[{"role": "user", "content": "hello"}],
        execution_context=context,
    )

    async def forbidden(**kwargs):
        raise AssertionError("server-prepared context must remain authoritative")

    runner = AgentRunner(prepare_execution_context=forbidden)
    prepared = await runner.prepare(request)

    assert prepared.request is request
    assert prepared.request.execution_context is context
