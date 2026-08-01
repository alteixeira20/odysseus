"""Behavior-neutral contracts for the strangled agent runtime package."""

import ast
import inspect
import json
from pathlib import Path

from src import agent_loop
from src.agent import api as agent_api
from src.agent.config import AgentSettingsSnapshot
from src.agent.contracts import AgentLimits, AgentRunRequest
from src.agent.events import AgentEvent, encode_legacy_sse, run_status_event
from src.agent.providers.errors import is_transient_error, stream_error_details
from src.agent.supervision.finalizer import empty_response_fallback
from src.agent.rounds.stream_consumer import stream_with_idle_status
from src.agent.supervision.loop_breaker import detect_runaway_call
from src.agent.telemetry.metrics import compute_final_metrics


LEGACY_STREAM_PARAMETERS = (
    "endpoint_url",
    "model",
    "messages",
    "headers",
    "temperature",
    "max_tokens",
    "prompt_type",
    "max_rounds",
    "max_tool_calls",
    "context_length",
    "active_document",
    "active_email",
    "session_id",
    "disabled_tools",
    "owner",
    "relevant_tools",
    "fallbacks",
    "plan_mode",
    "approved_plan",
    "tool_policy",
    "workspace",
    "forced_tools",
    "uploaded_files",
    "workload",
    "_is_teacher_run",
    "shell_enabled",
    "execution_context",
)


def test_legacy_stream_signature_is_frozen():
    signature = inspect.signature(agent_loop.stream_agent_loop)
    assert tuple(signature.parameters) == LEGACY_STREAM_PARAMETERS
    assert signature.parameters["temperature"].default == 0.3
    assert signature.parameters["max_tokens"].default == 4096
    assert signature.parameters["workload"].default == "foreground"
    assert signature.parameters["shell_enabled"].default is None
    assert signature.parameters["execution_context"].default is None
    assert tuple(inspect.signature(agent_api.stream_agent_loop).parameters) == (
        LEGACY_STREAM_PARAMETERS
    )


def test_legacy_compatibility_symbols_remain_importable():
    for name in (
        "stream_agent_loop",
        "TOOL_SECTIONS",
        "_classify_agent_request",
        "_compute_final_metrics",
        "_append_tool_results",
        "_insert_before_latest_user",
    ):
        assert hasattr(agent_loop, name), name
    assert agent_loop._compute_final_metrics is compute_final_metrics
    assert agent_loop._detect_runaway_call is detect_runaway_call
    assert agent_loop._empty_response_fallback is empty_response_fallback
    assert agent_loop._is_transient_error is is_transient_error
    assert agent_loop._stream_error_details is stream_error_details
    assert agent_loop._run_status_event is run_status_event
    assert agent_loop._stream_with_idle_status is stream_with_idle_status


def test_agent_request_can_capture_legacy_inputs_without_runtime_side_effects():
    messages = [{"role": "user", "content": "hello"}]
    request = AgentRunRequest.from_legacy_arguments(
        endpoint_url="https://provider.invalid/v1",
        model="test-model",
        messages=messages,
        max_rounds=4,
        max_tool_calls=9,
        session_id="session-1",
        owner="owner",
        workspace="/tmp/workspace",
        shell_enabled=True,
    )

    assert request.endpoint_url == "https://provider.invalid/v1"
    assert request.model == "test-model"
    assert request.messages == messages
    assert request.limits == AgentLimits(max_rounds=4, max_tool_calls=9)
    assert request.contexts.workspace == "/tmp/workspace"
    assert request.policy.shell_enabled is True


def test_typed_events_encode_the_existing_sse_wire_format():
    event = AgentEvent.typed(
        "tool_start",
        tool="bash",
        command="pwd",
        ephemeral=True,
    )
    assert encode_legacy_sse(event) == (
        "data: "
        + json.dumps(
            {
                "type": "tool_start",
                "tool": "bash",
                "command": "pwd",
                "ephemeral": True,
            }
        )
        + "\n\n"
    )
    assert encode_legacy_sse(AgentEvent.delta("ready")) == (
        'data: {"delta": "ready"}\n\n'
    )
    assert encode_legacy_sse(AgentEvent.done()) == "data: [DONE]\n\n"


def test_settings_snapshot_normalizes_invalid_values_without_config_io():
    snapshot = AgentSettingsSnapshot.from_mapping(
        {
            "agent_stream_timeout_seconds": "not-a-number",
            "agent_input_token_budget": "-10",
            "agent_input_token_hard_max": "999999999",
            "agent_max_tool_calls": "7",
            "skill_max_injected": "0",
            "agent_verifier_subagent": "yes",
        }
    )

    assert snapshot.stream_timeout_seconds == 300
    assert snapshot.input_token_budget == 0
    assert snapshot.input_token_hard_max == 1_000_000
    assert snapshot.max_tool_calls == 7
    assert snapshot.skill_max_injected == 0
    assert snapshot.verifier_enabled is True


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            yield node.module or ""


def test_agent_package_respects_dependency_boundaries():
    root = Path(__file__).resolve().parents[1] / "src" / "agent"
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        imported = tuple(_imports(path))
        assert not any(name == "routes" or name.startswith("routes.") for name in imported), relative
        if relative.parts[:1] == ("routing",):
            assert not any(
                name.startswith(("src.agent.prompting", "src.agent.execution"))
                for name in imported
            ), relative
        if relative.parts[:1] == ("prompting",):
            assert not any(name == "routes" or name.startswith("routes.") for name in imported), relative
        if relative.parts[:1] == ("tools",):
            assert "src.agent.orchestrator" not in imported, relative


def test_agent_loop_facade_does_not_import_http_routes():
    path = Path(agent_loop.__file__).resolve()
    imported = tuple(_imports(path))
    assert not any(
        name == "routes" or name.startswith("routes.")
        for name in imported
    )


def test_tool_policy_does_not_import_the_legacy_orchestrator():
    policy_path = (
        Path(agent_loop.__file__).resolve().parent / "tool_policy.py"
    )
    assert "src.agent_loop" not in tuple(_imports(policy_path))


def test_chat_route_depends_on_stable_agent_api():
    root = Path(__file__).resolve().parents[1]
    imported = tuple(_imports(root / "routes" / "chat_routes.py"))
    assert "src.agent.api" in imported
    assert "src.agent_loop" not in imported


def test_runtime_callers_use_stable_agent_api_not_legacy_stream():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for package in ("routes", "src"):
        for path in (root / package).rglob("*.py"):
            if path == root / "src" / "agent" / "api.py":
                continue
            tree = ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
            )
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module == "src.agent_loop"
                    and any(
                        alias.name == "stream_agent_loop"
                        for alias in node.names
                    )
                ):
                    offenders.append(str(path.relative_to(root)))
    assert offenders == []


async def test_typed_api_projects_request_to_legacy_runtime(monkeypatch):
    captured = {}

    def fake_legacy(**arguments):
        async def generate():
            captured.update(arguments)
            yield 'data: {"delta": "ready"}\n\n'
            yield "data: [DONE]\n\n"

        return generate()

    monkeypatch.setattr(agent_api, "_legacy_stream", fake_legacy)
    request = AgentRunRequest.from_legacy_arguments(
        endpoint_url="https://provider.invalid/v1",
        model="test-model",
        messages=[{"role": "user", "content": "hello"}],
        max_rounds=3,
        relevant_tools={"read_file"},
        workspace="/tmp/workspace",
        shell_enabled=False,
    )

    events = [event async for event in agent_api.stream(request)]

    assert events[-1] == "data: [DONE]\n\n"
    assert captured["max_rounds"] == 3
    assert captured["relevant_tools"] == {"read_file"}
    assert captured["workspace"] == "/tmp/workspace"
    assert captured["shell_enabled"] is False
