"""Behavioral authority, effect-policy, registry, and alias contracts."""

from dataclasses import FrozenInstanceError
import inspect

import pytest

from src.agent.rounds.tool_calls import normalize_tool_calls
from src.agent.runtime_v2.authority import (
    HOST_AUTHORIZATIONS,
    prepare_execution_context,
)
from src.agent.runtime_v2.contracts import (
    ApprovalDecision,
    RunBudgets,
    ToolResult,
    ToolResultStatus,
)
from src.agent.runtime_v2.effect_policy import EFFECT_POLICY
from src.agent.runtime_v2.executor import execute_normalized_tool_call
from src.agent.runtime_v2.tool_definitions import (
    MIGRATED_CANONICAL_NAMES,
    MIGRATED_LEGACY_NAMES,
)
from src.agent.tools.bootstrap import TOOL_REGISTRY
from src.agent_tools import ToolBlock
from src import bg_jobs
from src.execution_policy import ExecutionMode
from src.tool_execution import execute_tool_block


def context(
    tmp_path,
    *,
    mode="disabled",
    owner="owner",
    session="session",
    disabled=(),
    workspace_write=True,
):
    token = None
    if mode == "host":
        token = HOST_AUTHORIZATIONS.issue(
            owner_id=owner,
            session_id=session,
        ).token
    prepared, reason = prepare_execution_context(
        owner_id=owner,
        session_id=session,
        requested_mode=mode,
        selected_workspace=str(tmp_path),
        budgets=RunBudgets(),
        tool_catalog_revision=TOOL_REGISTRY.revision,
        host_authorization_token=token,
        workspace_write=workspace_write,
        disabled_tools=TOOL_REGISTRY.canonicalize_names(disabled, None),
    )
    assert reason == "requested"
    return prepared


def normalize(name, arguments, prepared):
    return normalize_tool_calls(
        [ToolBlock(name, "", arguments=arguments)],
        [],
        execution_context=prepared,
        provider_name="test-provider",
    )[0]


def test_execution_context_and_authority_snapshot_are_immutable(tmp_path):
    prepared = context(tmp_path, mode="sandboxed")
    with pytest.raises(FrozenInstanceError):
        prepared.execution_mode = ExecutionMode.HOST
    with pytest.raises(FrozenInstanceError):
        prepared.execution_root.path = "/"
    with pytest.raises(FrozenInstanceError):
        prepared.authority_grant.revision = "forged"


def test_host_authorization_is_owner_bound_one_use_and_explicit_denial_wins(tmp_path):
    issued = HOST_AUTHORIZATIONS.issue(owner_id="owner", session_id="one")
    first, reason = prepare_execution_context(
        owner_id="owner",
        session_id="one",
        requested_mode="host",
        selected_workspace=str(tmp_path),
        budgets=RunBudgets(),
        tool_catalog_revision=TOOL_REGISTRY.revision,
        host_authorization_token=issued.token,
    )
    assert first.execution_mode is ExecutionMode.HOST
    assert reason == "requested"

    replay, replay_reason = prepare_execution_context(
        owner_id="owner",
        session_id="one",
        requested_mode="host",
        selected_workspace=str(tmp_path),
        budgets=RunBudgets(),
        tool_catalog_revision=TOOL_REGISTRY.revision,
        host_authorization_token=issued.token,
    )
    assert replay.execution_mode is ExecutionMode.DISABLED
    assert replay_reason == "host_authorization_missing_or_consumed"

    denied_token = HOST_AUTHORIZATIONS.issue(owner_id="owner", session_id="two")
    denied, _ = prepare_execution_context(
        owner_id="owner",
        session_id="two",
        requested_mode="disabled",
        selected_workspace=str(tmp_path),
        budgets=RunBudgets(),
        tool_catalog_revision=TOOL_REGISTRY.revision,
        host_authorization_token=denied_token.token,
    )
    assert denied.execution_mode is ExecutionMode.DISABLED
    later, _ = prepare_execution_context(
        owner_id="owner",
        session_id="two",
        requested_mode="host",
        selected_workspace=str(tmp_path),
        budgets=RunBudgets(),
        tool_catalog_revision=TOOL_REGISTRY.revision,
        host_authorization_token=denied_token.token,
    )
    assert later.execution_mode is ExecutionMode.DISABLED


def test_concurrent_run_contexts_cannot_share_authority_or_workspace(tmp_path):
    left_root = tmp_path / "left"
    right_root = tmp_path / "right"
    left_root.mkdir()
    right_root.mkdir()
    left = context(left_root, mode="sandboxed", session="left")
    right = context(right_root, mode="sandboxed", session="right")
    assert left.run_id != right.run_id
    assert left.cancellation_token.identity != right.cancellation_token.identity
    assert left.execution_root.path != right.execution_root.path
    assert left.authority_grant.revision != right.authority_grant.revision


@pytest.mark.parametrize(
    "command",
    [
        "sudo id",
        "apt-get install package",
        "systemctl restart service",
        "docker run image",
        "git push origin main",
        "cat ~/.ssh/id_ed25519",
        "rm -rf build",
        "curl -X POST --data value https://example.test/resource",
        "unclassified-host-program --do-something",
    ],
)
def test_sensitive_or_opaque_host_effects_require_approval(tmp_path, command):
    prepared = context(tmp_path, mode="host")
    definition = TOOL_REGISTRY.resolve("run_host_command", prepared)
    arguments = definition.validate_arguments({"command": command})
    effects = definition.resolve_effects(arguments, prepared)
    outcome = EFFECT_POLICY.evaluate(
        prepared,
        effects,
        approval_policy=definition.approval_policy,
    )
    assert outcome.decision is ApprovalDecision.REQUIRE_APPROVAL


def test_justified_read_only_host_command_is_allowed(tmp_path):
    prepared = context(tmp_path, mode="host")
    definition = TOOL_REGISTRY.resolve("run_host_command", prepared)
    arguments = definition.validate_arguments({"command": "pwd"})
    outcome = EFFECT_POLICY.evaluate(
        prepared,
        definition.resolve_effects(arguments, prepared),
        approval_policy=definition.approval_policy,
    )
    assert outcome.decision is ApprovalDecision.ALLOW


def test_provider_schemas_are_canonical_and_host_is_contextual(tmp_path):
    sandbox = context(tmp_path, mode="sandboxed")
    sandbox_names = {
        schema["function"]["name"]
        for schema in TOOL_REGISTRY.function_schemas(sandbox)
    }
    assert MIGRATED_LEGACY_NAMES.isdisjoint(sandbox_names)
    assert "run_sandbox_command" in sandbox_names
    assert "run_host_command" not in sandbox_names

    host = context(tmp_path, mode="host", session="host-schema")
    host_names = {
        schema["function"]["name"]
        for schema in TOOL_REGISTRY.function_schemas(host)
    }
    assert "run_host_command" in host_names
    assert "run_sandbox_command" not in host_names
    assert (MIGRATED_CANONICAL_NAMES - {"run_sandbox_command"}) <= host_names


@pytest.mark.asyncio
async def test_disabled_alias_is_denied_by_canonical_executor(tmp_path):
    prepared = context(tmp_path, disabled={"grep"})
    call = normalize("rg", {"pattern": "needle"}, prepared)
    assert call.canonical_name == "search_text"
    result = await execute_normalized_tool_call(call, prepared)
    assert result.status is ToolResultStatus.DENIED
    assert result.error.code == "tool_disabled"


@pytest.mark.asyncio
async def test_model_cannot_select_host_with_tool_arguments(tmp_path):
    prepared = context(tmp_path, mode="sandboxed")
    for arguments in (
        {"command": "pwd", "host": True},
        {"command": "pwd", "target": "host"},
    ):
        call = normalize("bash", arguments, prepared)
        assert call.canonical_name == "run_sandbox_command"
        assert call.normalization_error
        result = await execute_normalized_tool_call(call, prepared)
        assert result.status is ToolResultStatus.ERROR
        assert result.error.code == "argument_validation_error"


def test_result_validation_rejects_handler_specific_or_mismatched_shapes(tmp_path):
    prepared = context(tmp_path)
    definition = TOOL_REGISTRY.resolve("read_files", prepared)
    with pytest.raises(ValueError, match="non-ToolResult"):
        definition.validate_result({"output": "ambiguous"})
    with pytest.raises(ValueError, match="returned result for"):
        definition.validate_result(
            ToolResult(
                call_id="call",
                canonical_name="search_text",
                status=ToolResultStatus.SUCCESS,
                data={},
            )
        )


def test_migrated_handlers_are_explicitly_context_bound_without_ambient_reads():
    forbidden = {
        "get_active_workspace",
        "get_active_execution_mode",
        "os.getcwd",
        "_active_workspace",
        "_active_execution_mode",
    }
    for name in MIGRATED_CANONICAL_NAMES:
        definition = TOOL_REGISTRY.get(name)
        parameters = inspect.signature(definition.handler).parameters
        assert tuple(parameters)[:2] == ("arguments", "context"), name
        source = inspect.getsource(definition.handler)
        assert forbidden.isdisjoint(
            token for token in forbidden if token in source
        ), name


@pytest.mark.asyncio
async def test_structured_delete_waits_for_exact_effect_approval(tmp_path):
    target = tmp_path / "obsolete.txt"
    target.write_text("remove me\n", encoding="utf-8")
    prepared = context(tmp_path)
    call = normalize(
        "apply_patch",
        {
            "patch_text": (
                "*** Begin Patch\n"
                "*** Delete File: obsolete.txt\n"
                "*** End Patch"
            )
        },
        prepared,
    )

    waiting = await execute_normalized_tool_call(call, prepared)
    assert waiting.status is ToolResultStatus.APPROVAL_REQUIRED
    assert target.exists()
    delete_effect = next(
        effect
        for effect in waiting.data["effects"]
        if effect["kind"] == "filesystem.delete"
    )

    from src.agent.runtime_v2.approvals import EFFECT_APPROVALS

    approval_id = waiting.data["approval"]["approval_id"]
    EFFECT_APPROVALS.decide(
        approval_id,
        owner_id=prepared.owner_id,
        decision="allow",
    )
    committed = await execute_normalized_tool_call(
        call,
        prepared,
        approval_id=approval_id,
    )
    assert committed.status is ToolResultStatus.SUCCESS
    assert not target.exists()
    assert any(effect.kind == "filesystem.delete" for effect in committed.committed_effects)


@pytest.mark.asyncio
async def test_legacy_read_alias_enters_v2_without_ambient_workspace(tmp_path, monkeypatch):
    target = tmp_path / "example.txt"
    target.write_text("hello\n", encoding="utf-8")
    prepared = context(tmp_path)

    def ambient_access_forbidden():
        raise AssertionError("migrated tool read ambient workspace")

    monkeypatch.setattr(
        "src.tool_execution.get_active_workspace",
        ambient_access_forbidden,
    )
    _, projection = await execute_tool_block(
        ToolBlock("read_file", "example.txt"),
        execution_context=prepared,
        allowed_tools={"read_file"},
    )
    assert projection["tool_result"]["canonical_name"] == "read_files"
    assert projection["completion_state"] == "success"


def test_background_job_captures_the_exact_immutable_authority_snapshot(tmp_path, monkeypatch):
    prepared = context(tmp_path, mode="host", session="background")
    from src.agent.runtime_v2.workspace_service import WORKSPACE_SERVICE

    jobs_dir = tmp_path / "jobs"
    saved = {}

    class Process:
        pid = 4242

    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs_dir)
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(bg_jobs, "_load", lambda: {})
    monkeypatch.setattr(bg_jobs, "_save", lambda jobs: saved.update(jobs))
    monkeypatch.setattr(bg_jobs, "find_bash", lambda: "/bin/bash")
    monkeypatch.setattr(bg_jobs, "detached_popen_kwargs", lambda: {})
    monkeypatch.setattr(
        WORKSPACE_SERVICE,
        "revision",
        lambda root: prepared.execution_root.workspace_revision,
    )
    monkeypatch.setattr(bg_jobs.subprocess, "Popen", lambda *args, **kwargs: Process())

    record = bg_jobs.launch(
        "pwd",
        session_id=prepared.session_id,
        cwd=prepared.execution_root.path,
        owner=prepared.owner_id,
        execution_mode=prepared.execution_mode.value,
        execution_context=prepared,
    )
    assert record["id"] in saved
    assert record["run_id"] == prepared.run_id
    assert record["execution_target"] == prepared.execution_mode.value
    assert record["execution_root"] == prepared.execution_root.path
    assert record["workspace_revision"] == prepared.execution_root.workspace_revision
    assert record["authority_revision"] == prepared.authority_grant.revision
    assert record["cancellation_identity"] == prepared.cancellation_token.identity
    assert record["deadline"] == record["started_at"] + record["max_runtime_s"]
    assert record["bounded_output_chars"] > 0
