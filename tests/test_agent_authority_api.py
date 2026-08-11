from types import SimpleNamespace

import pytest

from src.agent import api as agent_api
from src.agent.contracts import AgentAuthorityRequest
from src.agent.runner import AgentRunner, PreparedAuthority
from src.agent.runtime_v2.contracts import Capability
from src.agent.tools.bootstrap import TOOL_REGISTRY
from src.execution_policy import ExecutionMode


class _Source:
    value = "selected_workspace"


class _Grant:
    def __init__(self):
        self.revision = "authority-rev"
        self.capabilities = frozenset(
            {
                Capability.WORKSPACE_READ,
                Capability.WORKSPACE_WRITE,
                Capability.PROCESS_WORKSPACE_WRITE,
            }
        )

    def allows(self, capability):
        return capability in self.capabilities


def _context():
    root = SimpleNamespace(
        path="/tmp/workspace",
        source=_Source(),
        workspace_revision="workspace-rev",
    )
    return SimpleNamespace(
        execution_mode=ExecutionMode.SANDBOXED,
        execution_root=root,
        authority_grant=_Grant(),
        run_id="run-123",
    )


@pytest.mark.asyncio
async def test_stable_api_delegates_authority_request_to_runner(monkeypatch):
    request = AgentAuthorityRequest(owner_id="owner", session_id="session")
    expected = PreparedAuthority(context=_context(), reason="requested")
    captured = []

    class FakeRunner:
        async def prepare_authority(self, received):
            captured.append(received)
            return expected

    monkeypatch.setattr(agent_api, "DEFAULT_AGENT_RUNNER", FakeRunner())

    result = await agent_api.prepare_authority(request)

    assert result is expected
    assert captured == [request]


@pytest.mark.asyncio
async def test_runner_forwards_server_authority_inputs_without_rebinding(monkeypatch):
    captured = {}
    lease = object()
    context = _context()

    async def fake_prepare(**kwargs):
        captured.update(kwargs)
        return context, "requested"

    monkeypatch.setattr("src.agent.runner.get_setting", lambda key, default=None: default)
    monkeypatch.setattr(
        "src.agent.runner.blocked_tools_for_owner",
        lambda owner: {"write_file"} if owner == "owner" else set(),
    )
    runner = AgentRunner(prepare_execution_context=fake_prepare)
    request = AgentAuthorityRequest(
        owner_id="owner",
        session_id="session",
        requested_mode=ExecutionMode.SANDBOXED,
        selected_workspace="/tmp/workspace",
        max_rounds=9,
        max_tool_calls=13,
        plan_mode=False,
        host_authorization_token="one-use-token",
        conversation_id="conversation",
        turn_lease=lease,
        workspace_write=True,
        process_workspace_write=True,
        disabled_tools=frozenset({"generate_image"}),
        defer_ownership=True,
    )

    prepared = await runner.prepare_authority(request)

    assert prepared.context is context
    assert prepared.reason == "requested"
    assert captured["owner_id"] == "owner"
    assert captured["session_id"] == "session"
    assert captured["requested_mode"] is ExecutionMode.SANDBOXED
    assert captured["selected_workspace"] == "/tmp/workspace"
    assert captured["host_authorization_token"] == "one-use-token"
    assert captured["conversation_id"] == "conversation"
    assert captured["turn_lease"] is lease
    assert captured["workspace_write"] is True
    assert captured["process_workspace_write"] is True
    assert captured["defer_ownership"] is True
    assert captured["tool_catalog_revision"] == TOOL_REGISTRY.revision
    assert captured["budgets"].max_rounds == 9
    assert captured["budgets"].max_tool_calls == 13
    assert captured["budgets"].max_provider_requests == 27
    expected_disabled = TOOL_REGISTRY.canonicalize_names(
        {"generate_image", "write_file"}, None
    )
    assert captured["disabled_tools"] == expected_disabled


@pytest.mark.asyncio
async def test_plan_mode_is_reapplied_inside_authority_boundary(monkeypatch):
    captured = {}

    async def fake_prepare(**kwargs):
        captured.update(kwargs)
        return _context(), "requested"

    monkeypatch.setattr("src.agent.runner.get_setting", lambda key, default=None: default)
    monkeypatch.setattr("src.agent.runner.blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(
        "src.agent.runner.plan_mode_disabled_tools",
        lambda: {"bash", "write_file"},
    )
    runner = AgentRunner(prepare_execution_context=fake_prepare)

    await runner.prepare_authority(
        AgentAuthorityRequest(
            owner_id="owner",
            session_id="session",
            plan_mode=True,
            disabled_tools=frozenset(),
        )
    )

    expected = TOOL_REGISTRY.canonicalize_names({"bash", "write_file"}, None)
    assert expected <= captured["disabled_tools"]


def test_prepared_authority_owns_ui_projection():
    payload = PreparedAuthority(context=_context(), reason="requested").event_payload()

    assert payload == {
        "type": "execution_authority",
        "mode": "sandboxed",
        "reason": "requested",
        "workspace": True,
        "ephemeral": True,
        "execution_root": "/tmp/workspace",
        "root_source": "selected_workspace",
        "workspace_revision": "workspace-rev",
        "workspace_snapshot_policy": (
            "nonexecuting_git_metadata_and_bounded_workspace_content; "
            "strong_revision_required_for_approval"
        ),
        "authority_revision": "authority-rev",
        "capabilities": [
            "process_workspace_write",
            "workspace_read",
            "workspace_write",
        ],
        "workspace_write_granted": True,
        "process_workspace_write_granted": True,
        "run_id": "run-123",
    }
