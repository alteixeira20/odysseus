"""Adversarial production-path contracts for the daily-development runtime."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import secrets

import pytest

from src import agent_loop, agent_runs
from src.agent.rounds.tool_calls import normalize_tool_calls
from src.agent.runtime_v2.approvals import (
    ApprovalRecordState,
    EFFECT_APPROVALS,
    EffectApprovalError,
)
from src.agent.runtime_v2.authority import (
    HOST_AUTHORIZATIONS,
    prepare_execution_context,
)
from src.agent.runtime_v2.contracts import (
    Capability,
    RunBudgets,
    ToolResult,
    ToolResultStatus,
)
from src.agent.runtime_v2.events import runtime_event_from_payload
from src.agent.runtime_v2.executor import execute_normalized_tool_call
from src.agent.runtime_v2.ownership import OwnershipError, RUN_OWNERSHIP
from src.agent.runtime_v2.workspace_service import (
    WORKSPACE_SERVICE,
    WorkspaceConflict,
    WorkspaceError,
)
from src.agent.tools.bootstrap import TOOL_REGISTRY
from src.agent_tools import ToolBlock
from src.process_sandbox import redact_sensitive_output, sandbox_capability
from src.tool_execution import execute_tool_block


def context(
    root: Path,
    *,
    mode: str = "disabled",
    workspace_write: bool = False,
    session: str | None = None,
):
    session_id = session or f"dependability-{secrets.token_urlsafe(8)}"
    token = None
    if mode == "host":
        token = HOST_AUTHORIZATIONS.issue(
            owner_id="owner",
            session_id=session_id,
        ).token
    prepared, reason = prepare_execution_context(
        owner_id="owner",
        session_id=session_id,
        requested_mode=mode,
        selected_workspace=str(root),
        budgets=RunBudgets(wall_clock_seconds=30, idle_seconds=10),
        tool_catalog_revision=TOOL_REGISTRY.revision,
        host_authorization_token=token,
        workspace_write=workspace_write,
    )
    assert reason == "requested"
    return prepared


def normalize(
    prepared,
    name: str,
    arguments,
    *,
    provider_round: int = 0,
):
    return normalize_tool_calls(
        [ToolBlock(name, "", arguments=arguments)],
        [],
        execution_context=prepared,
        provider_name="dependability-provider",
        provider_round=provider_round,
    )[0]


def bind_tools(prepared, names: set[str], *, label: str) -> None:
    revision = hashlib.sha256(label.encode()).hexdigest()
    RUN_OWNERSHIP.bind_effective_tools(
        prepared,
        names=frozenset(names),
        revision=revision,
    )


def approval_id(result: ToolResult) -> str:
    return str(result.data["approval"]["approval_id"])


def delete_call(prepared, path: str):
    return normalize(
        prepared,
        "patch_workspace",
        {"operations": [{"type": "delete", "path": path}]},
    )


def runtime_payloads(events: list[str]) -> list[dict]:
    payloads = []
    for event in events:
        if not event.startswith("data: {"):
            continue
        value = json.loads(event[6:])
        if value.get("version") == 2:
            payloads.append(value)
    return payloads


def test_selected_writable_repository_is_inspection_only_by_default(tmp_path):
    prepared = context(tmp_path)

    assert prepared.execution_root.writable is True
    assert prepared.authority_grant.allows(Capability.WORKSPACE_READ)
    assert not prepared.authority_grant.allows(Capability.WORKSPACE_WRITE)
    names = {
        schema["function"]["name"]
        for schema in TOOL_REGISTRY.function_schemas(prepared)
    }
    assert "read_files" in names
    assert "patch_workspace" not in names


def test_process_local_runtime_refuses_multiple_workers(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(RuntimeError, match="exactly one runtime worker"):
        agent_runs.enforce_single_runtime_worker()
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    agent_runs.enforce_single_runtime_worker()


def test_process_output_redaction_covers_json_bearer_and_service_tokens():
    rendered, count = redact_sensitive_output(
        '{"api_key":"synthetic-json-secret"}\n'
        "Authorization: Bearer synthetic-bearer-secret\n"
        "sk-ABCDEFGHIJKLMNOPQRSTUV"
    )
    assert "synthetic-json-secret" not in rendered
    assert "synthetic-bearer-secret" not in rendered
    assert "sk-ABCDEFGHIJKLMNOPQRSTUV" not in rendered
    assert count == 3


@pytest.mark.asyncio
async def test_final_boundary_rejects_unexposed_alias_fenced_legacy_and_mcp_calls(tmp_path):
    (tmp_path / "example.txt").write_text("hello\n", encoding="utf-8")
    prepared = context(tmp_path)
    bind_tools(prepared, {"read_files"}, label="read-only-round")

    allowed = normalize(
        prepared,
        "read_file",
        {"requests": [{"path": "example.txt"}]},
    )
    assert (await execute_normalized_tool_call(allowed, prepared)).status is ToolResultStatus.SUCCESS

    for alias in ("rg", "grep"):
        forged = normalize(prepared, alias, {"pattern": "hello"})
        denied = await execute_normalized_tool_call(forged, prepared)
        assert denied.status is ToolResultStatus.DENIED
        assert denied.error.code == "tool_not_available"

    legacy_call = normalize(prepared, "manage_notes", {"action": "list"})
    _, legacy = await execute_tool_block(
        ToolBlock("manage_notes", "{\"action\":\"list\"}"),
        execution_context=prepared,
        normalized_call=legacy_call,
        allowed_tools={"read_files"},
    )
    assert legacy["error_type"] == "tool_not_available"

    mcp_name = "mcp__untrusted_server__apparently_read_only"
    mcp_call = normalize(prepared, mcp_name, {})
    _, mcp = await execute_tool_block(
        ToolBlock(mcp_name, "{}", arguments={}),
        execution_context=prepared,
        normalized_call=mcp_call,
        allowed_tools={"read_files"},
    )
    assert mcp["error_type"] == "tool_not_available"


@pytest.mark.asyncio
async def test_read_only_sandbox_cannot_mutate_or_read_common_workspace_secret(tmp_path):
    if not sandbox_capability().available:
        pytest.skip(sandbox_capability().reason)
    secret_value = "synthetic_daily_runtime_secret_72819"
    (tmp_path / ".env").write_text(
        f"API_KEY={secret_value}\n",
        encoding="utf-8",
    )
    prepared = context(tmp_path, mode="sandboxed")

    mutate = normalize(
        prepared,
        "run_sandbox_command",
        {"command": "printf changed > should-not-exist.txt"},
    )
    mutation_result = await execute_normalized_tool_call(mutate, prepared)
    assert mutation_result.status is ToolResultStatus.ERROR
    assert not (tmp_path / "should-not-exist.txt").exists()

    read_secret = normalize(
        prepared,
        "run_sandbox_command",
        {"command": "cat .env"},
    )
    secret_result = await execute_normalized_tool_call(read_secret, prepared)
    rendered = json.dumps(secret_result.as_dict())
    assert secret_value not in rendered
    assert "API_KEY=" not in str(secret_result.data.get("stdout") or "")

    redact = normalize(
        prepared,
        "run_sandbox_command",
        {"command": "printf 'AKIAABCDEFGHIJKLMNOP'"},
    )
    redact_result = await execute_normalized_tool_call(redact, prepared)
    assert redact_result.status is ToolResultStatus.SUCCESS
    assert "AKIAABCDEFGHIJKLMNOP" not in json.dumps(redact_result.as_dict())
    assert "<redacted-sensitive-output>" in str(redact_result.data.get("stdout") or "")


@pytest.mark.asyncio
async def test_granted_shell_mutation_advances_workspace_snapshot(tmp_path):
    if not sandbox_capability().available:
        pytest.skip(sandbox_capability().reason)
    prepared = context(tmp_path, mode="sandboxed", workspace_write=True)
    before = WORKSPACE_SERVICE.revision(str(tmp_path))
    call = normalize(
        prepared,
        "run_sandbox_command",
        {"command": "printf changed > generated.txt"},
    )

    result = await execute_normalized_tool_call(call, prepared)

    assert result.status is ToolResultStatus.SUCCESS
    assert (tmp_path / "generated.txt").read_text(encoding="utf-8") == "changed"
    assert result.data["workspace_mutated"] is True
    assert result.data["workspace_revision_before"] == before
    assert result.data["workspace_revision"] != before


@pytest.mark.asyncio
async def test_host_process_authority_does_not_grant_workspace_mutation(tmp_path):
    if not sandbox_capability().available:
        pytest.skip(sandbox_capability().reason)
    prepared = context(tmp_path, mode="host", workspace_write=False)
    call = normalize(
        prepared,
        "run_host_command",
        {"command": "printf blocked > host-should-not-exist.txt"},
    )
    waiting = await execute_normalized_tool_call(call, prepared)
    assert waiting.status is ToolResultStatus.APPROVAL_REQUIRED
    request_id = approval_id(waiting)
    EFFECT_APPROVALS.decide(request_id, owner_id="owner", decision="allow")

    result = await execute_normalized_tool_call(
        call,
        prepared,
        approval_id=request_id,
    )

    assert result.status is ToolResultStatus.ERROR
    assert not (tmp_path / "host-should-not-exist.txt").exists()
    assert EFFECT_APPROVALS.state(request_id) is ApprovalRecordState.CONSUMED


@pytest.mark.asyncio
async def test_approval_is_exact_atomic_and_one_use(tmp_path):
    target = tmp_path / "obsolete.txt"
    target.write_text("old\n", encoding="utf-8")
    prepared = context(tmp_path, workspace_write=True)
    call = delete_call(prepared, "obsolete.txt")
    waiting = await execute_normalized_tool_call(call, prepared)
    request_id = approval_id(waiting)
    EFFECT_APPROVALS.decide(request_id, owner_id="owner", decision="allow")

    committed = await execute_normalized_tool_call(
        call,
        prepared,
        approval_id=request_id,
    )
    replayed = await execute_normalized_tool_call(
        call,
        prepared,
        approval_id=request_id,
    )

    assert committed.status is ToolResultStatus.SUCCESS
    assert not target.exists()
    assert replayed.status is ToolResultStatus.DENIED
    assert EFFECT_APPROVALS.state(request_id) is ApprovalRecordState.CONSUMED


@pytest.mark.asyncio
async def test_changed_arguments_invalidate_granted_approval(tmp_path):
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")
    (tmp_path / "second.txt").write_text("second", encoding="utf-8")
    prepared = context(tmp_path, workspace_write=True)
    call = delete_call(prepared, "first.txt")
    waiting = await execute_normalized_tool_call(call, prepared)
    request_id = approval_id(waiting)
    EFFECT_APPROVALS.decide(request_id, owner_id="owner", decision="allow")
    changed = replace(
        call,
        arguments={"operations": [{"type": "delete", "path": "second.txt"}]},
    )

    denied = await execute_normalized_tool_call(
        changed,
        prepared,
        approval_id=request_id,
    )

    assert denied.status is ToolResultStatus.DENIED
    assert denied.error.code == "effect_approval_invalid"
    assert (tmp_path / "first.txt").exists()
    assert (tmp_path / "second.txt").exists()
    assert EFFECT_APPROVALS.state(request_id) is ApprovalRecordState.INVALIDATED


@pytest.mark.asyncio
async def test_superseded_turn_cannot_grant_a_pending_approval(tmp_path):
    (tmp_path / "obsolete.txt").write_text("old", encoding="utf-8")
    prepared = context(tmp_path, workspace_write=True)
    call = delete_call(prepared, "obsolete.txt")
    waiting = await execute_normalized_tool_call(call, prepared)
    request_id = approval_id(waiting)

    RUN_OWNERSHIP.claim_turn(
        owner_id=prepared.owner_id,
        conversation_id=prepared.conversation_id,
    )

    with pytest.raises(EffectApprovalError, match="superseded"):
        EFFECT_APPROVALS.decide(
            request_id,
            owner_id=prepared.owner_id,
            decision="allow",
        )
    assert EFFECT_APPROVALS.state(request_id) is ApprovalRecordState.INVALIDATED
    assert (tmp_path / "obsolete.txt").exists()


@pytest.mark.asyncio
async def test_workspace_change_invalidates_approval_before_or_after_grant(tmp_path):
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")
    prepared = context(tmp_path, workspace_write=True)
    call = delete_call(prepared, "first.txt")
    waiting = await execute_normalized_tool_call(call, prepared)
    before_grant = approval_id(waiting)
    (tmp_path / "external.txt").write_text("changed", encoding="utf-8")
    with pytest.raises(EffectApprovalError, match="workspace changed"):
        EFFECT_APPROVALS.decide(before_grant, owner_id="owner", decision="allow")
    assert EFFECT_APPROVALS.state(before_grant) is ApprovalRecordState.INVALIDATED

    (tmp_path / "external.txt").unlink()
    prepared = context(
        tmp_path,
        workspace_write=True,
        session=f"approval-after-{secrets.token_urlsafe(6)}",
    )
    call = delete_call(prepared, "first.txt")
    waiting = await execute_normalized_tool_call(call, prepared)
    after_grant = approval_id(waiting)
    EFFECT_APPROVALS.decide(after_grant, owner_id="owner", decision="allow")
    (tmp_path / "external.txt").write_text("changed again", encoding="utf-8")
    denied = await execute_normalized_tool_call(
        call,
        prepared,
        approval_id=after_grant,
    )
    assert denied.status is ToolResultStatus.DENIED
    assert (tmp_path / "first.txt").exists()
    assert EFFECT_APPROVALS.state(after_grant) is ApprovalRecordState.INVALIDATED


@pytest.mark.asyncio
async def test_empty_provider_after_completed_tool_is_incomplete_without_replay(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(
        agent_loop,
        "_build_system_prompt",
        lambda messages, *args, **kwargs: (
            [{"role": "system", "content": "SYSTEM"}] + list(messages),
            [],
        ),
    )
    provider_calls = 0
    tool_calls = 0

    async def provider(*args, **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            yield 'data: {"delta":"```read_file\\nexample.txt\\n```"}\n\n'
        else:
            yield 'data: {"type":"finish","reason":"stop"}\n\n'
        yield "data: [DONE]\n\n"

    async def execute(call, execution_context, **kwargs):
        nonlocal tool_calls
        tool_calls += 1
        return ToolResult(
            call_id=call.call_id,
            canonical_name=call.canonical_name,
            status=ToolResultStatus.SUCCESS,
            data={"text": "preserved observation"},
            backend="dependability-test",
        )

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    monkeypatch.setattr(
        "src.agent.execution.batch_runner.execute_normalized_tool_call",
        execute,
    )
    prepared = context(tmp_path)
    events = [
        event
        async for event in agent_loop.stream_agent_loop(
            "http://provider.invalid",
            "plain-model",
            [{"role": "user", "content": "Read and continue."}],
            relevant_tools={"read_file"},
            owner="owner",
            workspace=str(tmp_path),
            execution_context=prepared,
            max_rounds=3,
            _is_teacher_run=True,
        )
    ]
    typed = runtime_payloads(events)
    states = [
        item["payload"]
        for item in typed
        if item["type"] == "run_state"
    ]
    diagnostics = [
        item["payload"]
        for item in typed
        if item["type"] == "provider_attempts"
    ]

    assert provider_calls == 4
    assert tool_calls == 1
    assert states[-1]["state"] == "incomplete"
    assert states[-1]["reason"] == "provider_empty_recovery_exhausted"
    assert states[-1]["resumable"] is True
    assert diagnostics[-1]["final_disposition"] == "incomplete_resumable"
    assert diagnostics[-1]["attempts"][-1]["retry_decision"] == "recovery_exhausted"


@pytest.mark.asyncio
async def test_real_agent_batch_resumes_same_exact_call_after_approval(
    tmp_path,
    monkeypatch,
):
    target = tmp_path / "obsolete.txt"
    target.write_text("remove me\n", encoding="utf-8")
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10)
    monkeypatch.setattr(
        agent_loop,
        "_build_system_prompt",
        lambda messages, *args, **kwargs: (
            [{"role": "system", "content": "SYSTEM"}] + list(messages),
            [],
        ),
    )
    provider_calls = 0

    async def provider(*args, **kwargs):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 1:
            patch = (
                "```apply_patch\n"
                "*** Begin Patch\n"
                "*** Delete File: obsolete.txt\n"
                "*** End Patch\n"
                "```"
            )
            yield "data: " + json.dumps({"delta": patch}) + "\n\n"
        else:
            yield 'data: {"delta":"Deleted the obsolete file."}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    prepared = context(tmp_path, workspace_write=True)
    events = []
    approved_id = None
    async for event in agent_loop.stream_agent_loop(
        "http://provider.invalid",
        "plain-model",
        [{"role": "user", "content": "Delete obsolete.txt."}],
        relevant_tools={"apply_patch"},
        owner="owner",
        workspace=str(tmp_path),
        execution_context=prepared,
        max_rounds=3,
        _is_teacher_run=True,
    ):
        events.append(event)
        if event.startswith("data: {"):
            payload = json.loads(event[6:])
            nested = payload.get("payload") or {}
            if (
                payload.get("version") == 2
                and payload.get("type") == "run_state"
                and nested.get("state") == "waiting_approval"
            ):
                approved_id = nested["approval_id"]
                EFFECT_APPROVALS.decide(
                    approved_id,
                    owner_id=prepared.owner_id,
                    decision="allow",
                )

    typed = runtime_payloads(events)
    assert approved_id is not None
    assert provider_calls == 2
    assert not target.exists()
    assert EFFECT_APPROVALS.state(approved_id) is ApprovalRecordState.CONSUMED
    assert any(item["type"] == "tool_resumed" for item in typed)
    terminal = [
        item["payload"]
        for item in typed
        if item["type"] == "run_state" and item["payload"].get("terminal")
    ][-1]
    assert terminal["state"] == "completed"


@pytest.mark.asyncio
async def test_new_turn_blocks_old_text_migrated_and_legacy_calls(tmp_path):
    (tmp_path / "example.txt").write_text("hello", encoding="utf-8")
    session = f"stale-{secrets.token_urlsafe(8)}"
    prepared = context(tmp_path, session=session)
    migrated = normalize(
        prepared,
        "read_files",
        {"requests": [{"path": "example.txt"}]},
    )
    legacy = normalize(prepared, "manage_notes", {"action": "list"})
    release = asyncio.Event()

    async def producer():
        await release.wait()
        yield 'data: {"delta":"late old text"}\n\n'
        yield "data: [DONE]\n\n"

    run = agent_runs.start(
        session,
        producer(),
        owner="owner",
        execution_context=prepared,
    )
    await asyncio.sleep(0)
    RUN_OWNERSHIP.claim_turn(owner_id="owner", conversation_id=session)
    release.set()
    await run.task

    migrated_result = await execute_normalized_tool_call(migrated, prepared)
    _, legacy_result = await execute_tool_block(
        ToolBlock("manage_notes", "{\"action\":\"list\"}"),
        execution_context=prepared,
        normalized_call=legacy,
    )
    assert "late old text" not in "".join(run.buffer)
    assert migrated_result.status is ToolResultStatus.DENIED
    assert migrated_result.error.code == "stale_or_unauthorized_call"
    assert legacy_result["error_type"] == "stale_or_unauthorized_call"


@pytest.mark.asyncio
async def test_nonmigrated_dispatch_cannot_rebind_owner_session_or_root(tmp_path):
    primary = tmp_path / "primary"
    foreign = tmp_path / "foreign"
    primary.mkdir()
    foreign.mkdir()
    prepared = context(primary, session="legacy-boundary")
    bind_tools(prepared, {"manage_notes"}, label="legacy-boundary")
    legacy = normalize(prepared, "manage_notes", {"action": "list"})
    block = ToolBlock("manage_notes", '{"action":"list"}')

    attempts = (
        {"workspace": str(foreign)},
        {"owner": "different-owner"},
        {"session_id": "different-session"},
    )
    for forged in attempts:
        _, result = await execute_tool_block(
            block,
            execution_context=prepared,
            normalized_call=legacy,
            **forged,
        )
        assert result["error_type"] == "stale_or_unauthorized_call"


@pytest.mark.asyncio
async def test_exactly_one_candidate_can_commit_for_a_round(tmp_path):
    (tmp_path / "example.txt").write_text("hello", encoding="utf-8")
    prepared = context(tmp_path)
    bind_tools(prepared, {"read_files"}, label="candidate-round")
    RUN_OWNERSHIP.select_candidate(
        prepared,
        round_number=1,
        candidate_id="winner",
    )
    with pytest.raises(OwnershipError, match="already authoritative"):
        RUN_OWNERSHIP.select_candidate(
            prepared,
            round_number=1,
            candidate_id="loser",
        )
    winner_context = replace(prepared, candidate_id="winner")
    winner = normalize(
        winner_context,
        "read_files",
        {"requests": [{"path": "example.txt"}]},
        provider_round=1,
    )
    loser = replace(winner, candidate_id="loser")

    assert (await execute_normalized_tool_call(winner, winner_context)).status is ToolResultStatus.SUCCESS
    losing_result = await execute_normalized_tool_call(loser, winner_context)
    assert losing_result.status is ToolResultStatus.DENIED
    assert losing_result.error.code == "stale_or_unauthorized_call"

    winner_context.event_factory.select_candidate("loser")
    losing_event = winner_context.event_factory.create(
        "run_state",
        {"state": "running", "reason": "loser"},
    )
    with pytest.raises(OwnershipError, match="losing"):
        RUN_OWNERSHIP.validate_event(winner_context, losing_event)


def test_workspace_revision_transactions_metadata_and_budgets(tmp_path, monkeypatch):
    target = tmp_path / "script.sh"
    target.write_text("#!/bin/sh\necho before\n", encoding="utf-8")
    target.chmod(0o755)
    revision = WORKSPACE_SERVICE.revision(str(tmp_path))
    target.write_text("#!/bin/sh\necho external\n", encoding="utf-8")
    assert WORKSPACE_SERVICE.revision(str(tmp_path)) != revision

    with pytest.raises(WorkspaceConflict, match="duplicate target"):
        WORKSPACE_SERVICE.patch_workspace(
            str(tmp_path),
            {
                "operations": [
                    {"type": "write", "path": "script.sh", "content": "one"},
                    {"type": "write", "path": "./script.sh", "content": "two"},
                ]
            },
        )
    assert "external" in target.read_text(encoding="utf-8")

    WORKSPACE_SERVICE.patch_workspace(
        str(tmp_path),
        {"operations": [{"type": "write", "path": "script.sh", "content": "after\n"}]},
    )
    assert os.stat(target).st_mode & 0o111

    before_failure = target.read_text(encoding="utf-8")
    original_fsync = WORKSPACE_SERVICE._fsync_directory
    raised = False

    def fail_once_after_replace(path):
        nonlocal raised
        if not raised and os.path.realpath(path) == os.path.realpath(tmp_path):
            raised = True
            raise OSError("simulated directory fsync interruption")
        return original_fsync(path)

    monkeypatch.setattr(WORKSPACE_SERVICE, "_fsync_directory", fail_once_after_replace)
    with pytest.raises(WorkspaceError, match="originals restored"):
        WORKSPACE_SERVICE.patch_workspace(
            str(tmp_path),
            {"operations": [{"type": "write", "path": "script.sh", "content": "unsafe\n"}]},
        )
    assert target.read_text(encoding="utf-8") == before_failure

    journal_parent = os.path.realpath(WORKSPACE_SERVICE.transaction_parent(str(tmp_path)))
    assert os.path.commonpath((os.path.realpath(tmp_path), journal_parent)) != os.path.realpath(tmp_path)

    for index in range(20):
        (tmp_path / f"wide-{index:02}.txt").write_text("needle\n", encoding="utf-8")
    found = WORKSPACE_SERVICE.find_files(
        str(tmp_path),
        {
            "patterns": ["**/*"],
            "max_results": 20,
            "max_scan_entries": 3,
        },
    )
    searched = WORKSPACE_SERVICE.search_text(
        str(tmp_path),
        {
            "pattern": "needle",
            "fixed_string": True,
            "max_results": 20,
            "max_scan_entries": 4,
        },
    )
    assert found["budget_exhausted"] is True
    assert found["scan_complete"] is False
    assert found["scanned_entries"] <= 3
    assert searched["budget_exhausted"] is True
    assert searched["scan_complete"] is False
    assert searched["scanned_entries"] <= 4


def test_ripgrep_search_masks_secret_files_and_secret_shaped_output(tmp_path):
    if WORKSPACE_SERVICE._rg_available() is None:
        pytest.skip("ripgrep unavailable")
    (tmp_path / ".env").write_text(
        "needle=workspace-secret-value\n", encoding="utf-8"
    )
    (tmp_path / "credentials.json").write_text(
        '{"needle":"credential-secret"}\n', encoding="utf-8"
    )
    (tmp_path / "source.txt").write_text(
        "needle AKIAABCDEFGHIJKLMNOP\n", encoding="utf-8"
    )

    result = WORKSPACE_SERVICE.search_text(
        str(tmp_path),
        {"pattern": "needle", "fixed_string": True, "max_results": 20},
    )

    assert {item["path"] for item in result["matches"]} == {"source.txt"}
    assert "AKIAABCDEFGHIJKLMNOP" not in json.dumps(result)
    assert "<redacted-sensitive-output>" in json.dumps(result)
