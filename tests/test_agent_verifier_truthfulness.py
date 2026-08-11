import pytest

from src.agent.supervision.verifier import (
    VerificationStatus,
    parse_verification_response,
    run_verifier_subagent,
)


def test_verifier_parser_requires_explicit_success_marker():
    result = parse_verification_response("Looks good to me")
    assert result.status is VerificationStatus.UNKNOWN
    assert result.diagnostic == "missing_verification_marker"


def test_verifier_parser_accepts_exact_success():
    result = parse_verification_response(
        "Evidence matches the request.\nVERIFICATION: SUCCESS"
    )
    assert result.status is VerificationStatus.PASS
    assert result.findings == ()


def test_verifier_parser_preserves_failure_findings():
    result = parse_verification_response(
        "Two issues remain.\nVERIFICATION: FAIL: tests were not run; output file is missing"
    )
    assert result.status is VerificationStatus.FAIL
    assert result.findings == ("tests were not run", "output file is missing")


@pytest.mark.parametrize(
    "raw,diagnostic",
    [
        ("VERIFICATION: FAIL:", "empty_failure_reason"),
        ("VERIFICATION: MAYBE", "malformed_verification_marker"),
        ("VERIFICATION: SUCCESS extra", "malformed_verification_marker"),
    ],
)
def test_verifier_parser_fails_closed_on_malformed_protocol(raw, diagnostic):
    result = parse_verification_response(raw)
    assert result.status is VerificationStatus.UNKNOWN
    assert result.diagnostic == diagnostic


@pytest.mark.asyncio
async def test_verifier_transport_failure_is_unknown(monkeypatch):
    async def broken_call(**kwargs):
        raise TimeoutError("provider timed out")

    monkeypatch.setattr("src.llm_core.llm_call_async", broken_call)
    result = await run_verifier_subagent(
        "create the requested artifact",
        "[patch_workspace]\n-> success",
        endpoint_url="http://local",
        model="test-model",
        headers={},
    )
    assert result.status is VerificationStatus.UNKNOWN
    assert result.diagnostic == "verifier_call_failed:TimeoutError"
