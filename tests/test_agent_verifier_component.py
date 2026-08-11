import pytest

from src import agent_loop
from src.agent.supervision import verifier
from src.agent.supervision.verifier import (
    VerificationResult,
    VerificationStatus,
    _parse_verification,
    build_actions_snapshot,
    run_verifier_subagent,
    verify_completion_subagent,
)


def test_legacy_verifier_exports_are_aliases():
    assert agent_loop._build_actions_snapshot is build_actions_snapshot
    assert agent_loop._run_verifier_subagent is run_verifier_subagent


def test_action_snapshot_is_bounded_and_records_failures():
    snapshot = build_actions_snapshot(
        [
            {
                "tool": "bash",
                "command": "false",
                "output": "failed",
                "exit_code": 1,
            }
        ],
        limit=80,
    )

    assert "[bash] false (exit 1)" in snapshot
    assert "-> failed" in snapshot
    assert len(snapshot) <= 80


def test_verification_parser_distinguishes_pass_fail_and_unknown():
    passed = _parse_verification("Looks correct.\nVERIFICATION: SUCCESS")
    assert passed.status is VerificationStatus.PASS
    assert passed.findings == ()

    failed = _parse_verification(
        "Two issues.\nVERIFICATION: FAIL: tests were not run; output file is missing"
    )
    assert failed.status is VerificationStatus.FAIL
    assert failed.findings == ("tests were not run", "output file is missing")

    missing = _parse_verification("I think it is probably fine")
    assert missing.status is VerificationStatus.UNKNOWN
    assert missing.detail == "missing_verification_line"

    malformed = _parse_verification("VERIFICATION: MAYBE")
    assert malformed.status is VerificationStatus.UNKNOWN
    assert malformed.detail == "malformed_verification_line"


@pytest.mark.asyncio
async def test_provider_failure_is_unknown_not_success(monkeypatch):
    async def broken_call(**kwargs):
        raise TimeoutError("verifier timeout")

    import src.llm_core

    monkeypatch.setattr(src.llm_core, "llm_call_async", broken_call)
    result = await verify_completion_subagent(
        "change the file",
        "[patch_workspace] changed",
        endpoint_url="http://local",
        model="m",
        headers={},
    )
    assert result.status is VerificationStatus.UNKNOWN
    assert result.detail.startswith("provider_error:")


@pytest.mark.asyncio
async def test_legacy_adapter_fails_closed_when_verification_is_unknown(monkeypatch):
    async def unknown_result(*args, **kwargs):
        return VerificationResult(
            VerificationStatus.UNKNOWN,
            detail="malformed_verification_line",
        )

    monkeypatch.setattr(verifier, "verify_completion_subagent", unknown_result)
    findings = await run_verifier_subagent(
        "change the file",
        "[patch_workspace] changed",
        endpoint_url="http://local",
        model="m",
        headers={},
    )
    assert findings
    assert "could not be confirmed" in findings[0]


def test_effectful_compatibility_set_covers_external_and_workspace_writes():
    assert "patch_workspace" in verifier.EFFECTFUL_TOOLS
    assert "send_email" in verifier.EFFECTFUL_TOOLS
    assert "manage_calendar" in verifier.EFFECTFUL_TOOLS
    assert "serve_model" in verifier.EFFECTFUL_TOOLS
