from src.agent.contracts import AgentRunRequest
from src.agent.runner import AgentLoopCompatibilityBackend


def test_execution_mode_wins_over_legacy_shell_boolean_in_backend_projection():
    request = AgentRunRequest.from_legacy_arguments(
        endpoint_url="https://provider.invalid/v1",
        model="model-a",
        messages=[{"role": "user", "content": "hello"}],
        shell_enabled=True,
        execution_mode="disabled",
    )
    arguments = AgentLoopCompatibilityBackend.arguments(request)
    assert arguments["shell_enabled"] == "disabled"


def test_backend_projection_preserves_optional_none_semantics():
    request = AgentRunRequest.from_legacy_arguments(
        endpoint_url="https://provider.invalid/v1",
        model="model-a",
        messages=[{"role": "user", "content": "hello"}],
    )
    arguments = AgentLoopCompatibilityBackend.arguments(request)
    assert arguments["disabled_tools"] is None
    assert arguments["relevant_tools"] is None
    assert arguments["forced_tools"] is None
    assert arguments["fallbacks"] is None
    assert arguments["uploaded_files"] is None
