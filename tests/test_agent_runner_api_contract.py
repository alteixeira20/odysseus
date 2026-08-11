import inspect

import pytest

from src.agent import api as agent_api
from src.agent.contracts import AgentRunRequest


@pytest.mark.asyncio
async def test_legacy_api_signature_builds_typed_request_then_delegates(monkeypatch):
    captured = {}

    class FakeRunner:
        async def stream(self, request):
            captured["request"] = request
            yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_api, "DEFAULT_AGENT_RUNNER", FakeRunner())

    events = [
        event
        async for event in agent_api.stream_agent_loop(
            "https://provider.invalid/v1",
            "model-a",
            [{"role": "user", "content": "hello"}],
            session_id="session-a",
            owner="owner-a",
            relevant_tools={"read_file"},
            forced_tools={"read_file"},
            shell_enabled=False,
        )
    ]

    assert events == ["data: [DONE]\n\n"]
    request = captured["request"]
    assert isinstance(request, AgentRunRequest)
    assert request.session_id == "session-a"
    assert request.owner == "owner-a"
    assert request.policy.relevant_tools == frozenset({"read_file"})
    assert request.policy.forced_tools == frozenset({"read_file"})
    assert request.policy.shell_enabled is False


def test_public_stream_accepts_exactly_one_typed_request():
    signature = inspect.signature(agent_api.stream)
    assert tuple(signature.parameters) == ("request",)
