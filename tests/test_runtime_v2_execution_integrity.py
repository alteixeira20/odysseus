"""Adversarial contracts for Runtime V2 execution integrity boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

from src import agent_runs
from src.agent.rounds.tool_calls import normalize_tool_calls
from src.agent.execution.batch_runner import (
    BatchDisposition,
    ToolBatchRequest,
    ToolBatchRunner,
    ToolBatchState,
)
from src.agent.runtime_v2.approvals import (
    ApprovalRecordState,
    EFFECT_APPROVALS,
)
from src.agent.runtime_v2.authority import (
    HOST_AUTHORIZATIONS,
    prepare_execution_context,
)
from src.agent.runtime_v2.contracts import (
    ApprovalDecision,
    Capability,
    RunBudgets,
    ToolResultStatus,
)
from src.agent.runtime_v2.effect_policy import EFFECT_POLICY
from src.agent.runtime_v2.events import RuntimeEventFactory, runtime_event_from_payload
from src.agent.runtime_v2.executor import execute_normalized_tool_call
from src.agent.runtime_v2.ownership import OwnershipError
from src.agent.runtime_v2.workspace_service import (
    WORKSPACE_SERVICE,
    WorkspaceConflict,
    WorkspaceError,
)
from src.agent.tools.bootstrap import TOOL_REGISTRY
from src.agent.tools.registry import ToolRegistry
from src.agent_tools import ToolBlock
from src.agent_tools.subprocess_tools import BashExecutionResult


def _context(
    root: Path,
    *,
    mode: str = "disabled",
    session: str = "execution-integrity",
    workspace_write: bool = False,
    process_workspace_write: bool = False,
):
    token = None
    if mode == "host":
        token = HOST_AUTHORIZATIONS.issue(
            owner_id="owner",
            session_id=session,
        ).token
    context, reason = prepare_execution_context(
        owner_id="owner",
        session_id=session,
        requested_mode=mode,
        selected_workspace=str(root),
        budgets=RunBudgets(wall_clock_seconds=30, idle_seconds=10),
        tool_catalog_revision=TOOL_REGISTRY.revision,
        host_authorization_token=token,
        workspace_write=workspace_write,
        process_workspace_write=process_workspace_write,
    )
    assert reason == "requested"
    return context


def _call(context, name: str, arguments: dict):
    return normalize_tool_calls(
        [ToolBlock(name, "", arguments=arguments)],
        [],
        execution_context=context,
        provider_name="execution-integrity-test",
    )[0]


async def _approved_request(context, call):
    waiting = await execute_normalized_tool_call(call, context)
    assert waiting.status is ToolResultStatus.APPROVAL_REQUIRED
    approval_id = str(waiting.data["approval"]["approval_id"])
    EFFECT_APPROVALS.decide(
        approval_id,
        owner_id=context.owner_id,
        decision="allow",
    )
    return approval_id


async def _approval_batch_events(context, call, block, *, decision: str | None):
    state = ToolBatchState(
        messages=[],
        full_response="",
        total_tool_calls=0,
        tool_events=[],
        relevant_tools={"patch_workspace"},
    )
    request = ToolBatchRequest(
        tool_blocks=[block],
        converted_calls=[],
        used_native=False,
        round_response="",
        round_reasoning="",
        round_number=0,
        max_tool_calls=10,
        session_id=context.session_id,
        owner=context.owner_id,
        workspace=context.execution_root.path,
        disabled_tools=set(),
        allowed_tools={"patch_workspace"},
        normalized_calls=(call,),
        execution_context=context,
    )

    async def legacy_execution_forbidden(*args, **kwargs):
        raise AssertionError("Runtime V2 approval call entered the legacy executor")

    runner = ToolBatchRunner(
        request,
        state,
        execute_tool=legacy_execution_forbidden,
        format_result=lambda description, result: json.dumps(result),
        append_results=lambda *args, **kwargs: None,
        strip_tool_blocks=lambda value: value,
        bash_timeout_for_block=lambda value: None,
        effectful_tools={"patch_workspace"},
    )
    events = []
    async for event in runner.stream():
        events.append(event)
        if not event.startswith("data: {"):
            continue
        payload = json.loads(event[6:])
        nested = payload.get("payload") or {}
        if (
            payload.get("version") == 2
            and payload.get("type") == "run_state"
            and nested.get("state") == "waiting_approval"
            and decision is not None
        ):
            EFFECT_APPROVALS.decide(
                nested["approval_id"],
                owner_id=context.owner_id,
                decision=decision,
            )
    return runner, events


def test_untrusted_git_configuration_never_executes_during_internal_reads(
    tmp_path,
    monkeypatch,
):
    sentinel = tmp_path / "git-config-executed"
    helper = tmp_path / "sentinel-helper.sh"
    helper.write_text(
        f"#!/bin/sh\nprintf unsafe > {sentinel}\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    git_dir = tmp_path / ".git"
    hooks = git_dir / "hooks"
    hooks.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "config").write_text(
        textwrap.dedent(
            f"""
            [core]
                fsmonitor = {helper}
                hooksPath = {hooks}
            [diff "sentinel"]
                textconv = {helper}
                command = {helper}
            [diff]
                external = {helper}
            [credential]
                helper = !{helper}
            [alias]
                status = !{helper}
            """
        ),
        encoding="utf-8",
    )
    hook = hooks / "post-checkout"
    hook.write_text(helper.read_text(encoding="utf-8"), encoding="utf-8")
    hook.chmod(0o755)
    (tmp_path / ".gitattributes").write_text(
        "*.txt diff=sentinel\n",
        encoding="utf-8",
    )
    (tmp_path / "sample.txt").write_text("content\n", encoding="utf-8")

    def subprocess_forbidden(*args, **kwargs):
        raise AssertionError("internal workspace inspection invoked a subprocess")

    monkeypatch.setattr(subprocess, "Popen", subprocess_forbidden)
    revision = WORKSPACE_SERVICE.revision(str(tmp_path))
    found = WORKSPACE_SERVICE.find_files(
        str(tmp_path),
        {"patterns": ["**/*"], "max_results": 20},
    )
    read = WORKSPACE_SERVICE.read_files(
        str(tmp_path),
        {"requests": [{"path": "sample.txt"}]},
    )
    searched = WORKSPACE_SERVICE.search_text(
        str(tmp_path),
        {"pattern": "content", "fixed_string": True},
    )

    # Reads remain available, while executable Git configuration deliberately
    # makes process approvals fail closed instead of claiming a strong
    # behavioral snapshot.
    assert not WORKSPACE_SERVICE.revision_is_strong(revision)
    assert [item["path"] for item in found["files"]] == [
        ".gitattributes",
        "sample.txt",
        "sentinel-helper.sh",
    ]
    assert read["files"][0]["status"] == "success"
    assert searched["backend"] == "python_native"
    assert searched["matches"][0]["path"] == "sample.txt"
    assert not sentinel.exists()

    host_context = _context(
        tmp_path,
        mode="host",
        session="dangerous-git-config-host",
    )
    host_definition = TOOL_REGISTRY.resolve("run_host_command", host_context)
    with pytest.raises(RuntimeError, match="Git identity"):
        host_definition.resolve_effects(
            {"command": "git diff -- sample.txt"},
            host_context,
        )
    assert not sentinel.exists()


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "rg --pre 'touch nested-sentinel' needle .",
        "git status --short",
        "python3 -c 'print(1)'",
        "sh -c 'printf wrapped'",
    ],
)
def test_every_generic_host_command_requires_exact_approval(tmp_path, command):
    context = _context(tmp_path, mode="host", session=f"host-{abs(hash(command))}")
    definition = TOOL_REGISTRY.resolve("run_host_command", context)
    arguments = definition.validate_arguments({"command": command})
    effects = definition.resolve_effects(arguments, context)

    outcome = EFFECT_POLICY.evaluate(
        context,
        effects,
        approval_policy=definition.approval_policy,
    )

    assert outcome.decision is ApprovalDecision.REQUIRE_APPROVAL
    assert not (tmp_path / "nested-sentinel").exists()


@pytest.mark.asyncio
async def test_pre_effect_launch_failure_is_retryable_once_effect_can_start(
    tmp_path,
    monkeypatch,
):
    import src.agent.runtime_v2.process_service as process_service_module

    context = _context(tmp_path, mode="host", session="pre-effect-retry")
    call = _call(context, "run_host_command", {"command": "pwd"})
    approval_id = await _approved_request(context, call)

    async def fail_before_launch(*args, **kwargs):
        raise OSError("synthetic exec failure")

    monkeypatch.setattr(
        process_service_module,
        "_run_direct_bash",
        fail_before_launch,
    )
    failed = await execute_normalized_tool_call(
        call,
        context,
        approval_id=approval_id,
    )
    assert failed.status is ToolResultStatus.ERROR
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.FAILED_BEFORE_EFFECT
    assert failed.attempted_effects
    assert not failed.observed_effects
    assert not failed.committed_effects
    assert not failed.unknown_effects

    async def succeed(*args, **kwargs):
        kwargs["effect_started_cb"]()
        return BashExecutionResult(
            stdout=str(tmp_path) + "\n",
            stderr="",
            exit_code=0,
            state="completed",
            timeout_seconds=5,
            invocation_id="retry",
        )

    monkeypatch.setattr(process_service_module, "_run_direct_bash", succeed)
    retried = await execute_normalized_tool_call(
        call,
        context,
        approval_id=approval_id,
    )
    assert retried.status is ToolResultStatus.SUCCESS
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.COMPLETED
    assert retried.observed_effects
    assert not retried.committed_effects


@pytest.mark.asyncio
async def test_post_start_interruption_records_unknown_effects_without_commit(
    tmp_path,
    monkeypatch,
):
    import src.agent.runtime_v2.process_service as process_service_module

    context = _context(tmp_path, mode="host", session="post-start-cancel")
    call = _call(context, "run_host_command", {"command": "pwd"})
    approval_id = await _approved_request(context, call)

    async def interrupt_after_start(*args, **kwargs):
        kwargs["effect_started_cb"]()
        raise asyncio.CancelledError

    monkeypatch.setattr(
        process_service_module,
        "_run_direct_bash",
        interrupt_after_start,
    )
    result = await execute_normalized_tool_call(
        call,
        context,
        approval_id=approval_id,
    )

    assert result.status is ToolResultStatus.CANCELLED
    assert result.attempted_effects
    assert result.observed_effects
    assert result.unknown_effects
    assert not result.committed_effects
    assert (
        EFFECT_APPROVALS.state(approval_id)
        is ApprovalRecordState.FAILED_AFTER_UNKNOWN_EFFECT
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ("deny", BatchDisposition.APPROVAL_DENIED),
        (None, BatchDisposition.APPROVAL_EXPIRED),
    ],
)
async def test_denied_or_expired_approval_never_emits_false_running_transition(
    tmp_path,
    decision,
    expected,
):
    target = tmp_path / f"{expected.value}.txt"
    target.write_text("keep\n", encoding="utf-8")
    context = _context(
        tmp_path,
        session=f"batch-{expected.value}",
        workspace_write=True,
    )
    arguments = {"operations": [{"type": "delete", "path": target.name}]}
    block = ToolBlock("patch_workspace", "", arguments=arguments)
    call = _call(context, "patch_workspace", arguments)
    original_ttl = EFFECT_APPROVALS.ttl_seconds
    if decision is None:
        EFFECT_APPROVALS.ttl_seconds = 0.001
    try:
        runner, events = await _approval_batch_events(
            context,
            call,
            block,
            decision=decision,
        )
    finally:
        EFFECT_APPROVALS.ttl_seconds = original_ttl

    typed = [
        json.loads(event[6:])
        for event in events
        if event.startswith("data: {")
        and json.loads(event[6:]).get("version") == 2
    ]
    states = [
        item["payload"].get("state")
        for item in typed
        if item.get("type") == "run_state"
    ]
    assert runner.outcome is not None
    assert runner.outcome.disposition is expected
    assert states == ["waiting_approval"]
    assert target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    [
        "executable",
        "executable_removed",
        "interpreter",
        "environment",
        "git_config",
    ],
)
async def test_process_approval_invalidates_on_behavioral_identity_drift(
    tmp_path,
    monkeypatch,
    drift,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    interpreter = tmp_path / "approved-interpreter"
    interpreter.write_text("#!/bin/sh\nexec /bin/sh \"$@\"\n", encoding="utf-8")
    interpreter.chmod(0o755)
    executable = tmp_path / "approved-tool"
    executable.write_text(
        f"#!{interpreter}\nprintf original\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    git_dir = workspace / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
    context = _context(
        workspace,
        mode="host",
        session=f"identity-{drift}",
    )
    call = _call(
        context,
        "run_host_command",
        {"command": str(executable)},
    )
    approval_id = await _approved_request(context, call)

    if drift == "executable":
        executable.write_text("#!/bin/sh\nprintf changed\n", encoding="utf-8")
    elif drift == "executable_removed":
        executable.unlink()
    elif drift == "interpreter":
        interpreter.write_text("#!/bin/sh\nexit 9\n", encoding="utf-8")
    elif drift == "environment":
        monkeypatch.setenv("LANG", "execution-integrity-drift")
    else:
        (git_dir / "config").write_text(
            "[core]\n\tbare = false\n\tfsmonitor = changed-helper\n",
            encoding="utf-8",
        )

    denied = await execute_normalized_tool_call(
        call,
        context,
        approval_id=approval_id,
    )
    assert denied.status is ToolResultStatus.DENIED
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.INVALIDATED


@pytest.mark.asyncio
async def test_process_approval_invalidates_when_default_git_hook_changes(tmp_path):
    workspace = tmp_path / "workspace"
    hooks = workspace / ".git" / "hooks"
    hooks.mkdir(parents=True)
    (workspace / ".git" / "HEAD").write_text(
        "ref: refs/heads/main\n",
        encoding="utf-8",
    )
    (workspace / ".git" / "config").write_text(
        "[core]\n\tbare = false\n",
        encoding="utf-8",
    )
    hook = hooks / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)
    executable = tmp_path / "approved-tool"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    context = _context(workspace, mode="host", session="identity-git-hook")
    call = _call(
        context,
        "run_host_command",
        {"command": str(executable)},
    )
    approval_id = await _approved_request(context, call)

    hook.write_text("#!/bin/sh\nprintf changed\n", encoding="utf-8")

    denied = await execute_normalized_tool_call(
        call,
        context,
        approval_id=approval_id,
    )
    assert denied.status is ToolResultStatus.DENIED
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.INVALIDATED


def test_project_scale_workspace_can_establish_strong_process_identity(tmp_path):
    artifact = tmp_path / "project-artifact.bin"
    with artifact.open("wb") as handle:
        handle.seek(12 * 1024 * 1024 - 1)
        handle.write(b"\0")

    revision = WORKSPACE_SERVICE.revision(str(tmp_path))

    assert WORKSPACE_SERVICE.revision_is_strong(revision)


def test_patch_write_and_process_write_authorities_are_independent(tmp_path):
    patch_only = _context(
        tmp_path,
        mode="sandboxed",
        session="patch-only",
        workspace_write=True,
    )
    assert patch_only.authority_grant.allows(Capability.WORKSPACE_WRITE)
    assert not patch_only.authority_grant.allows(Capability.PROCESS_WORKSPACE_WRITE)
    definition = TOOL_REGISTRY.resolve("run_sandbox_command", patch_only)
    effects = definition.resolve_effects(
        {"command": "printf changed > generated.txt"},
        patch_only,
    )
    assert not any(effect.kind == "filesystem.process_write" for effect in effects)

    explicit_process_write = _context(
        tmp_path,
        mode="sandboxed",
        session="explicit-process-write",
        process_workspace_write=True,
    )
    effects = definition.resolve_effects(
        {"command": "printf changed > generated.txt"},
        explicit_process_write,
    )
    assert any(effect.kind == "filesystem.process_write" for effect in effects)
    assert (
        EFFECT_POLICY.evaluate(
            explicit_process_write,
            effects,
            approval_policy=definition.approval_policy,
        ).decision
        is ApprovalDecision.REQUIRE_APPROVAL
    )


def test_find_files_prefix_collision_paginates_without_omission_or_duplicate(tmp_path):
    (tmp_path / "a").mkdir()
    for relative in ("a/child.txt", "a.txt", "a0.txt", "z.txt"):
        (tmp_path / relative).write_text(relative, encoding="utf-8")
    request = {"patterns": ["**/*"], "max_results": 1}
    paths: list[str] = []
    while True:
        page = WORKSPACE_SERVICE.find_files(str(tmp_path), request)
        paths.extend(item["path"] for item in page["files"])
        if not page["continuation"]:
            break
        request = {
            "patterns": ["**/*"],
            "max_results": 1,
            "continuation": page["continuation"]["token"],
        }

    assert paths == ["a.txt", "a/child.txt", "a0.txt", "z.txt"]
    assert len(paths) == len(set(paths))


def test_budget_exhausted_empty_search_pages_always_continue_exactly(tmp_path):
    for index in range(8):
        content = "needle\n" if index == 7 else "nothing here\n"
        (tmp_path / f"file-{index}.txt").write_text(content, encoding="utf-8")
    base = {
        "pattern": "needle",
        "fixed_string": True,
        "max_results": 1,
        "max_scan_entries": 2,
    }
    request = dict(base)
    pages = []
    matches = []
    first_token = None
    while True:
        page = WORKSPACE_SERVICE.search_text(str(tmp_path), request)
        pages.append(page)
        matches.extend(page["matches"])
        continuation = page["continuation"]
        if first_token is None and continuation:
            first_token = continuation["token"]
        if not continuation:
            break
        request = {**base, "continuation": continuation["token"]}

    assert pages[0]["matches"] == []
    assert pages[0]["continuation"] is not None
    assert [(item["path"], item["line"]) for item in matches] == [
        ("file-7.txt", 1)
    ]
    assert first_token is not None
    (tmp_path / "new-file.txt").write_text("needle\n", encoding="utf-8")
    with pytest.raises(WorkspaceConflict, match="workspace revision"):
        WORKSPACE_SERVICE.search_text(
            str(tmp_path),
            {**base, "continuation": first_token},
        )


def test_one_entry_empty_search_pages_make_forward_progress(tmp_path):
    for index in range(5):
        (tmp_path / f"file-{index}.txt").write_text(
            "needle\n" if index == 4 else "no match\n",
            encoding="utf-8",
        )
    request = {
        "pattern": "needle",
        "fixed_string": True,
        "max_results": 1,
        "max_scan_entries": 1,
    }
    pages = []
    for _ in range(10):
        page = WORKSPACE_SERVICE.search_text(str(tmp_path), request)
        pages.append(page)
        if not page["continuation"]:
            break
        request = {
            **request,
            "continuation": page["continuation"]["token"],
        }
    else:
        pytest.fail("one-entry continuation did not make forward progress")

    assert [
        match["path"]
        for page in pages
        for match in page["matches"]
    ] == ["file-4.txt"]


def test_runtime_events_fail_closed_without_exact_conversation_and_turn():
    payload = {
        "version": 2,
        "event_id": "event",
        "run_id": "run",
        "conversation_id": "",
        "turn_id": "turn",
        "candidate_id": None,
        "sequence": 1,
        "timestamp": "now",
        "type": "run_state",
        "caused_by": None,
        "payload": {},
    }
    assert runtime_event_from_payload(payload) is None
    assert runtime_event_from_payload({key: value for key, value in payload.items() if key != "turn_id"}) is None
    with pytest.raises(ValueError, match="conversation"):
        RuntimeEventFactory("run", conversation_id="", turn_id="turn")


def test_catalogue_revision_binds_security_relevant_behavior_versions():
    baseline = TOOL_REGISTRY.revision
    definitions = {definition.name: definition for definition in TOOL_REGISTRY}
    target = definitions["run_host_command"]
    definitions[target.name] = replace(
        target,
        security_policy_version=target.security_policy_version + "-changed",
    )
    changed = ToolRegistry(definitions)

    assert changed.revision != baseline


def test_stale_transaction_journal_cannot_replay_against_another_filesystem_identity(
    tmp_path,
):
    target = tmp_path / "target.txt"
    target.write_text("current\n", encoding="utf-8")
    transaction_id = "stale-checkout"
    journal = Path(WORKSPACE_SERVICE.transaction_parent(str(tmp_path))) / transaction_id
    backups = journal / "backups"
    backups.mkdir(parents=True)
    (backups / "0.bin").write_text("stale\n", encoding="utf-8")
    identity = WORKSPACE_SERVICE._filesystem_identity(str(tmp_path))
    identity["inode"] += 1
    (journal / "manifest.json").write_text(
        json.dumps(
            {
                "version": 2,
                "transaction_id": transaction_id,
                "state": "prepared",
                "root": str(tmp_path.resolve()),
                "filesystem_identity": identity,
                "changes": [
                    {
                        "path": target.name,
                        "existed": True,
                        "backup": "backups/0.bin",
                        "delete": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    try:
        with pytest.raises(WorkspaceError, match="identity mismatch"):
            WORKSPACE_SERVICE.recover_transactions(str(tmp_path))
        assert target.read_text(encoding="utf-8") == "current\n"
    finally:
        shutil.rmtree(journal, ignore_errors=True)


@pytest.mark.asyncio
async def test_prepare_then_commit_preserves_valid_run_and_rejects_concurrent_replacement():
    session = "atomic-replacement-execution-integrity"
    old_release = asyncio.Event()
    new_release = asyncio.Event()

    async def old_source():
        await old_release.wait()
        yield "data: [DONE]\n\n"

    old = agent_runs.start(session, old_source(), owner="owner")
    invalid_preparation = agent_runs.prepare_turn(session_id=session, owner="owner")
    assert invalid_preparation.expected_run is old
    assert not old.task.done()

    first = agent_runs.prepare_turn(session_id=session, owner="owner")
    second = agent_runs.prepare_turn(session_id=session, owner="owner")

    async def first_source():
        await new_release.wait()
        yield "data: [DONE]\n\n"

    async def losing_source():
        yield "data: [DONE]\n\n"

    committed_mutations = []
    winner = agent_runs.start(
        session,
        first_source(),
        owner="owner",
        prepared_turn=first,
        commit_callback=lambda: committed_mutations.append("winner"),
    )
    loser = losing_source()
    with pytest.raises(OwnershipError, match="changed while"):
        agent_runs.start(
            session,
            loser,
            owner="owner",
            prepared_turn=second,
            commit_callback=lambda: committed_mutations.append("loser"),
        )
    await loser.aclose()
    await asyncio.sleep(0)
    assert not winner.task.done()
    assert agent_runs.is_active(session)
    assert committed_mutations == ["winner"]

    new_release.set()
    await winner.task
    old_release.set()


def test_two_processes_cannot_share_process_local_runtime_state(tmp_path):
    state_root = tmp_path / "runtime-state"
    state_root.mkdir()
    code = textwrap.dedent(
        """
        import time
        from src.agent_runs import enforce_single_runtime_worker
        enforce_single_runtime_worker()
        print("READY", flush=True)
        time.sleep(30)
        """
    )
    environment = os.environ.copy()
    environment["ODYSSEUS_RUNTIME_STATE_DIR"] = str(state_root)
    first = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert first.stdout is not None
        assert first.stdout.readline().strip() == "READY"
        second = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
        assert second.returncode != 0
        assert "already owns this Runtime V2 state directory" in second.stderr
    finally:
        first.terminate()
        try:
            first.wait(timeout=5)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait(timeout=5)


@pytest.mark.asyncio
async def test_cancellation_immediately_before_kernel_spawn_proves_no_child(
    tmp_path, monkeypatch
):
    import src.agent_tools.subprocess_tools as subprocess_tools

    context = _context(tmp_path, mode="host", session="cancel-before-spawn")
    call = _call(context, "run_host_command", {"command": "sleep 30"})
    approval_id = await _approved_request(context, call)
    original_mark_spawning = EFFECT_APPROVALS.mark_spawning
    spawn_called = False

    def cancel_after_spawn_claim(*args, **kwargs):
        original_mark_spawning(*args, **kwargs)
        raise asyncio.CancelledError

    async def forbidden_spawn(*args, **kwargs):
        nonlocal spawn_called
        spawn_called = True
        raise AssertionError("kernel spawn must not be attempted")

    monkeypatch.setattr(EFFECT_APPROVALS, "mark_spawning", cancel_after_spawn_claim)
    monkeypatch.setattr(subprocess_tools.asyncio, "create_subprocess_exec", forbidden_spawn)
    result = await execute_normalized_tool_call(call, context, approval_id=approval_id)

    assert result.status is ToolResultStatus.CANCELLED
    assert spawn_called is False
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.FAILED_BEFORE_EFFECT


@pytest.mark.asyncio
async def test_cancellation_during_kernel_spawn_owns_and_reaps_returned_process(
    tmp_path, monkeypatch
):
    import src.agent_tools.subprocess_tools as subprocess_tools

    context = _context(tmp_path, mode="host", session="cancel-during-spawn")
    call = _call(context, "run_host_command", {"command": "sleep 30"})
    approval_id = await _approved_request(context, call)
    entered = asyncio.Event()
    release = asyncio.Event()
    terminated = []

    class FakeProcess:
        pid = 424242
        returncode = None

        async def wait(self):
            self.returncode = -15
            return self.returncode

    async def pending_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return FakeProcess()

    async def terminate_group(pid):
        terminated.append(pid)

    monkeypatch.setattr(subprocess_tools.asyncio, "create_subprocess_exec", pending_spawn)
    monkeypatch.setattr(subprocess_tools, "_terminate_owned_group", terminate_group)
    task = asyncio.create_task(
        execute_normalized_tool_call(call, context, approval_id=approval_id)
    )
    await entered.wait()
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.SPAWNING
    task.cancel()
    release.set()
    result = await task

    assert result.status is ToolResultStatus.CANCELLED
    assert terminated == [424242]
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.FAILED_AFTER_UNKNOWN_EFFECT
    replay = await execute_normalized_tool_call(call, context, approval_id=approval_id)
    assert replay.status is ToolResultStatus.DENIED


@pytest.mark.asyncio
async def test_repeated_cancellation_during_group_termination_cannot_orphan_child(
    tmp_path, monkeypatch
):
    import src.agent_tools.subprocess_tools as subprocess_tools

    context = _context(tmp_path, mode="host", session="cancel-during-termination")
    call = _call(context, "run_host_command", {"command": "sleep 30"})
    approval_id = await _approved_request(context, call)
    spawn_entered = asyncio.Event()
    spawn_release = asyncio.Event()
    termination_entered = asyncio.Event()
    termination_release = asyncio.Event()
    termination_finished = asyncio.Event()

    class FakeProcess:
        pid = 424243
        returncode = None

        async def wait(self):
            self.returncode = -15
            return self.returncode

    async def pending_spawn(*args, **kwargs):
        spawn_entered.set()
        await spawn_release.wait()
        return FakeProcess()

    async def slow_termination(pid):
        assert pid == 424243
        termination_entered.set()
        await termination_release.wait()
        termination_finished.set()
        return ("sigterm",)

    monkeypatch.setattr(subprocess_tools.asyncio, "create_subprocess_exec", pending_spawn)
    monkeypatch.setattr(subprocess_tools, "_terminate_owned_group", slow_termination)
    task = asyncio.create_task(
        execute_normalized_tool_call(call, context, approval_id=approval_id)
    )
    await spawn_entered.wait()
    task.cancel()
    spawn_release.set()
    await termination_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    termination_release.set()
    result = await task

    assert result.status is ToolResultStatus.CANCELLED
    assert termination_finished.is_set()
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.FAILED_AFTER_UNKNOWN_EFFECT


@pytest.mark.asyncio
async def test_pid_returned_before_executing_transition_is_reaped_and_burned(
    tmp_path, monkeypatch
):
    import src.agent_tools.subprocess_tools as subprocess_tools

    context = _context(tmp_path, mode="host", session="cancel-after-pid")
    call = _call(context, "run_host_command", {"command": "sleep 30"})
    approval_id = await _approved_request(context, call)
    terminated = []

    class FakeProcess:
        pid = 434343
        returncode = None

        async def wait(self):
            self.returncode = -15
            return self.returncode

    async def immediate_spawn(*args, **kwargs):
        return FakeProcess()

    async def terminate_group(pid):
        terminated.append(pid)

    def cancel_at_pid_boundary(candidate):
        assert EFFECT_APPROVALS.state(candidate) is ApprovalRecordState.SPAWNING
        raise asyncio.CancelledError

    monkeypatch.setattr(subprocess_tools.asyncio, "create_subprocess_exec", immediate_spawn)
    monkeypatch.setattr(subprocess_tools, "_terminate_owned_group", terminate_group)
    monkeypatch.setattr(EFFECT_APPROVALS, "mark_executing", cancel_at_pid_boundary)
    result = await execute_normalized_tool_call(call, context, approval_id=approval_id)

    assert result.status is ToolResultStatus.CANCELLED
    assert terminated == [434343]
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.FAILED_AFTER_UNKNOWN_EFFECT


@pytest.mark.asyncio
async def test_superseded_run_during_spawn_reaps_child_before_return(tmp_path, monkeypatch):
    import src.agent_tools.subprocess_tools as subprocess_tools
    from src.agent.runtime_v2.ownership import RUN_OWNERSHIP

    context = _context(tmp_path, mode="host", session="supersede-during-spawn")
    call = _call(context, "run_host_command", {"command": "sleep 30"})
    approval_id = await _approved_request(context, call)
    entered = asyncio.Event()
    release = asyncio.Event()
    terminated = []

    class FakeProcess:
        pid = 444444
        returncode = None

        async def wait(self):
            self.returncode = -15
            return self.returncode

    async def pending_spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return FakeProcess()

    async def terminate_group(pid):
        terminated.append(pid)

    monkeypatch.setattr(subprocess_tools.asyncio, "create_subprocess_exec", pending_spawn)
    monkeypatch.setattr(subprocess_tools, "_terminate_owned_group", terminate_group)
    task = asyncio.create_task(
        execute_normalized_tool_call(call, context, approval_id=approval_id)
    )
    await entered.wait()
    RUN_OWNERSHIP.claim_turn(owner_id="owner", conversation_id=context.conversation_id)
    release.set()
    result = await task

    assert result.status is ToolResultStatus.ERROR
    assert terminated == [444444]
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.FAILED_AFTER_UNKNOWN_EFFECT


@pytest.mark.parametrize(
    "command",
    [
        "PATH=/tmp/bin:$PATH helper",
        "PYTHONPATH=/tmp/plugins python -c 'import plugin'",
        "source ./setup.sh",
        "cmd=$(cat command-name); \"$cmd\"",
        "npm test",
        "pnpm test",
        "make target",
        "python -c 'import installed_package'",
    ],
)
def test_dynamic_host_commands_are_exact_opaque_not_falsely_sealed(tmp_path, command):
    (tmp_path / "setup.sh").write_text("true\n", encoding="utf-8")
    context = _context(tmp_path, mode="host", session=f"opaque-{abs(hash(command))}")
    definition = TOOL_REGISTRY.resolve("run_host_command", context)
    arguments = definition.validate_arguments({"command": command})
    effect = definition.resolve_effects(arguments, context)[0]

    assert effect.opaque is True
    assert effect.metadata["binding"] == "exact_opaque_command"
    assert effect.metadata["complete_dependency_seal"] is False
    assert effect.metadata["known_dependency_snapshot"]


@pytest.mark.asyncio
async def test_explicit_sourced_file_drift_invalidates_known_dependency_snapshot(tmp_path):
    setup = tmp_path / "setup.sh"
    setup.write_text("VALUE=approved\n", encoding="utf-8")
    context = _context(tmp_path, mode="host", session="sourced-drift")
    call = _call(
        context,
        "run_host_command",
        {"command": "source ./setup.sh; printf '%s' \"$VALUE\""},
    )
    approval_id = await _approved_request(context, call)
    setup.write_text("VALUE=changed\n", encoding="utf-8")

    denied = await execute_normalized_tool_call(
        call, context, approval_id=approval_id
    )
    assert denied.status is ToolResultStatus.DENIED
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.INVALIDATED


def test_background_marker_is_normalized_before_effect_and_identity(tmp_path):
    context = _context(tmp_path, mode="host", session="normalized-background")
    call = _call(
        context,
        "run_host_command",
        {"command": "#!bg\nprintf normalized"},
    )
    assert call.arguments == {"command": "printf normalized", "background": True}
    definition = TOOL_REGISTRY.resolve("run_host_command", context)
    effects = definition.resolve_effects(call.arguments, context)
    assert call.arguments["command"] == "printf normalized"
    assert effects[0].metadata["background"] is True


@pytest.mark.asyncio
async def test_workspace_snapshot_reuse_is_off_loop_and_constant_for_approval_checks(tmp_path):
    for index in range(50):
        (tmp_path / f"file-{index}.txt").write_text("content\n", encoding="utf-8")
    before = WORKSPACE_SERVICE.snapshot_build_count(str(tmp_path))
    heartbeat = []

    async def beat():
        started = time.perf_counter()
        await asyncio.sleep(0)
        heartbeat.append(time.perf_counter() - started)

    beat_task = asyncio.create_task(beat())
    cold_started = time.perf_counter()
    cold = await WORKSPACE_SERVICE.revision_async(str(tmp_path))
    cold_latency = time.perf_counter() - cold_started
    await beat_task
    repeated_started = time.perf_counter()
    repeated = [WORKSPACE_SERVICE.revision(str(tmp_path)) for _ in range(10)]
    repeated_latency = time.perf_counter() - repeated_started

    assert repeated == [cold] * 10
    assert WORKSPACE_SERVICE.snapshot_build_count(str(tmp_path)) == before + 1
    assert repeated_latency < max(cold_latency, 0.05)
    assert heartbeat[0] < 0.05


@pytest.mark.asyncio
async def test_concurrent_detached_snapshot_checks_share_one_cold_build(tmp_path):
    for index in range(25):
        (tmp_path / f"concurrent-{index}.txt").write_text("content\n", encoding="utf-8")
    before = WORKSPACE_SERVICE.snapshot_build_count(str(tmp_path))

    revisions = await asyncio.gather(
        *(WORKSPACE_SERVICE.revision_async(str(tmp_path)) for _ in range(8))
    )

    assert len(set(revisions)) == 1
    assert WORKSPACE_SERVICE.snapshot_build_count(str(tmp_path)) == before + 1


def test_high_fanout_find_index_consumes_only_scan_budget(tmp_path, monkeypatch):
    WORKSPACE_SERVICE.revision(str(tmp_path))
    import src.agent.runtime_v2.workspace_service as workspace_module

    real_scandir = workspace_module.os.scandir
    consumed = 0

    class FakeEntry:
        def __init__(self, index):
            self.name = f"entry-{index:09d}.txt"
            self.path = str(tmp_path / self.name)

        def stat(self, *, follow_symlinks=False):
            class Info:
                st_mode = 0o100644
            return Info()

    class FakeScandir:
        def __init__(self):
            self.index = 0

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed
            if self.index >= 1_000_000:
                raise StopIteration
            item = FakeEntry(self.index)
            self.index += 1
            consumed += 1
            return item

        def close(self):
            pass

    def bounded_scandir(path):
        if os.path.realpath(path) == os.path.realpath(tmp_path):
            return FakeScandir()
        return real_scandir(path)

    monkeypatch.setattr(workspace_module.os, "scandir", bounded_scandir)
    page = WORKSPACE_SERVICE.find_files(
        str(tmp_path),
        {
            "patterns": ["**/*"],
            "max_results": 10,
            "max_scan_entries": 7,
            "scan_timeout_seconds": 1,
        },
    )

    assert consumed == 7
    assert page["files"] == []
    assert page["budget_exhausted"] is True
    assert page["continuation"] is not None


def test_high_fanout_strong_snapshot_stops_at_identity_budget(tmp_path, monkeypatch):
    import src.agent.runtime_v2.workspace_service as workspace_module

    service = workspace_module.WorkspaceService()
    monkeypatch.setattr(service, "_MAX_REVISION_ENTRIES", 7)
    real_scandir = workspace_module.os.scandir
    consumed = 0

    class FakeEntry:
        def __init__(self, index):
            self.name = f"entry-{index:09d}"
            self.path = str(tmp_path / self.name)

        def stat(self, *, follow_symlinks=False):
            class Info:
                st_mode = 0o040755
                st_size = 0
                st_mtime_ns = 1
                st_ino = 100
            return Info()

    class FakeScandir:
        def __init__(self):
            self.index = 0

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed
            item = FakeEntry(self.index)
            self.index += 1
            consumed += 1
            return item

        def close(self):
            pass

    def bounded_scandir(path):
        if os.path.realpath(path) == os.path.realpath(tmp_path):
            return FakeScandir()
        return real_scandir(path)

    monkeypatch.setattr(workspace_module.os, "scandir", bounded_scandir)
    revision = service.revision(str(tmp_path))

    assert consumed == 8
    assert ".partial." in revision


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="detached host lifecycle uses POSIX process groups")
async def test_detached_approval_uses_normalized_command_and_reaches_final_state(
    tmp_path, monkeypatch
):
    from src import bg_jobs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs_dir)
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    context = _context(workspace, mode="host", session="detached-finalization")
    call = _call(
        context,
        "run_host_command",
        {"command": "#!bg\nprintf detached-complete"},
    )
    approval_id = await _approved_request(context, call)

    launched = await execute_normalized_tool_call(
        call, context, approval_id=approval_id
    )
    assert launched.status is ToolResultStatus.SUCCESS
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.DETACHED_EXECUTING
    job_id = launched.data["job_id"]
    deadline = time.monotonic() + 5
    record = None
    while time.monotonic() < deadline:
        record = bg_jobs.get(job_id)
        if record and record.get("detached_outcome") != "detached_executing":
            break
        await asyncio.sleep(0.02)

    assert record is not None
    assert record["command"] == "printf detached-complete"
    assert record["approval_identity"]
    assert record["complete_dependency_seal"] is False
    assert record["detached_outcome"] == "detached_completed"
    assert EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.DETACHED_COMPLETED


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="detached host lifecycle uses POSIX process groups")
async def test_concurrent_detached_runs_reuse_snapshot_and_all_finalize(tmp_path, monkeypatch):
    from src import bg_jobs

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs_dir)
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    before = WORKSPACE_SERVICE.snapshot_build_count(str(workspace))
    requests = []
    for index in range(4):
        context = _context(
            workspace,
            mode="host",
            session=f"concurrent-detached-{index}",
        )
        call = _call(
            context,
            "run_host_command",
            {"command": "#!bg\nsleep 0.05; printf complete"},
        )
        approval_id = await _approved_request(context, call)
        requests.append((context, call, approval_id))

    launched = await asyncio.gather(
        *(
            execute_normalized_tool_call(call, context, approval_id=approval_id)
            for context, call, approval_id in requests
        )
    )
    assert all(result.status is ToolResultStatus.SUCCESS for result in launched)
    assert WORKSPACE_SERVICE.snapshot_build_count(str(workspace)) == before + 1

    deadline = time.monotonic() + 5
    records = {}
    while time.monotonic() < deadline:
        records = bg_jobs.refresh()
        if all(
            records.get(result.data["job_id"], {}).get("detached_outcome")
            == "detached_completed"
            for result in launched
        ):
            break
        await asyncio.sleep(0.02)

    assert all(
        records[result.data["job_id"]]["detached_outcome"] == "detached_completed"
        for result in launched
    )
    assert all(
        EFFECT_APPROVALS.state(approval_id) is ApprovalRecordState.DETACHED_COMPLETED
        for _, _, approval_id in requests
    )
