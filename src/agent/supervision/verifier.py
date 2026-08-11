"""Independent completion verification for effectful agent work.

The verifier is intentionally advisory to orchestration, but its own failures
must never be indistinguishable from a successful verification. The typed
``VerificationResult`` API preserves PASS/FAIL/UNKNOWN, while the legacy
``run_verifier_subagent`` adapter fails closed by returning a concrete finding
for UNKNOWN so existing callers request another evidence-producing round.
"""

from dataclasses import dataclass
from enum import Enum
import logging
import re

from src.agent.providers.adapters.default import strip_think_blocks


logger = logging.getLogger(__name__)

# Legacy/non-Runtime-V2 tools whose successful use changes state or can create
# externally observable effects. Runtime-V2 calls are additionally detected by
# committed effect metadata in ToolBatchRunner, so this set is a compatibility
# backstop rather than the source of truth for new tools.
EFFECTFUL_TOOLS = {
    "create_document",
    "update_document",
    "edit_document",
    "suggest_document",
    "write_file",
    "edit_file",
    "apply_patch",
    "patch_workspace",
    "bash",
    "python",
    "run_sandbox_command",
    "run_host_command",
    "run_python",
    "send_email",
    "reply_to_email",
    "bulk_email",
    "archive_email",
    "delete_email",
    "mark_email_read",
    "unsubscribe_email",
    "manage_calendar",
    "manage_contact",
    "manage_documents",
    "manage_memory",
    "manage_notes",
    "manage_research",
    "manage_session",
    "manage_skills",
    "manage_tasks",
    "manage_settings",
    "manage_endpoints",
    "manage_mcp",
    "manage_webhooks",
    "manage_tokens",
    "send_to_session",
    "create_session",
    "download_model",
    "serve_model",
    "serve_preset",
    "stop_served_model",
    "cancel_download",
    "adopt_served_model",
    "api_call",
    "ui_control",
}
MAX_VERIFIER_ROUNDS = 2


class VerificationStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VerificationResult:
    status: VerificationStatus
    findings: tuple[str, ...] = ()
    detail: str = ""

    @property
    def confirmed(self) -> bool:
        return self.status is VerificationStatus.PASS


_UNKNOWN_FINDING = (
    "Independent verification was unavailable or malformed, so completion "
    "could not be confirmed. Re-check the requested deliverables with tools "
    "and produce fresh evidence before claiming the task is done."
)


def build_actions_snapshot(tool_events: list, limit: int = 8000) -> str:
    parts = []
    for event in tool_events:
        tool = event.get("tool", "?")
        command = (event.get("command") or "").strip()
        output = (event.get("output") or "").strip()
        exit_code = event.get("exit_code")
        head = f"[{tool}] {command}" if command else f"[{tool}]"
        exit_text = (
            f" (exit {exit_code})"
            if exit_code not in (None, 0)
            else ""
        )
        body = (
            output[:1200] + " …"
            if len(output) > 1200
            else output or "(no output)"
        )
        parts.append(f"{head}{exit_text}\n-> {body}")
    snapshot = "\n\n".join(parts)
    return snapshot[:limit] if len(snapshot) > limit else snapshot


def _parse_verification(raw: str) -> VerificationResult:
    text = strip_think_blocks(raw or "")
    verification_lines = [
        line.strip()
        for line in text.splitlines()
        if "VERIFICATION:" in line
    ]
    if not verification_lines:
        return VerificationResult(
            VerificationStatus.UNKNOWN,
            detail="missing_verification_line",
        )

    line = verification_lines[-1]
    if re.fullmatch(r"VERIFICATION:\s*SUCCESS\s*", line, flags=re.IGNORECASE):
        return VerificationResult(VerificationStatus.PASS)

    match = re.fullmatch(
        r"VERIFICATION:\s*FAIL:\s*(.+)",
        line,
        flags=re.IGNORECASE,
    )
    if not match:
        return VerificationResult(
            VerificationStatus.UNKNOWN,
            detail="malformed_verification_line",
        )

    findings = tuple(
        item.strip()
        for item in match.group(1).split(";")
        if item.strip()
    )
    if not findings:
        return VerificationResult(
            VerificationStatus.UNKNOWN,
            detail="empty_failure_findings",
        )
    return VerificationResult(VerificationStatus.FAIL, findings=findings)


async def verify_completion_subagent(
    instruction: str,
    actions_snapshot: str,
    *,
    endpoint_url: str,
    model: str,
    headers: dict,
) -> VerificationResult:
    """Return a typed verification outcome; transport/parser failures are UNKNOWN."""
    from src.llm_core import llm_call_async

    prompt = (
        "You are an independent verifier. Another assistant just claimed the "
        "following task is complete. Using ONLY the request and the record of "
        "what it actually did, decide whether that claim is correct. Be strict: "
        "only say SUCCESS if the work genuinely satisfies the request.\n\n"
        f"<user_request>\n{(instruction or '')[:4000]}\n</user_request>\n\n"
        f"<actions_taken>\n{actions_snapshot[:8000]}\n</actions_taken>\n\n"
        "<checklist>\n"
        "1. Every concrete deliverable the request asked for was actually produced\n"
        "2. Outputs/edits match what was asked — nothing missing, no extra or unrequested changes\n"
        "3. Tool results show success, not errors or empty output that got ignored\n"
        "4. Anything the request said to leave alone was left unchanged\n"
        "</checklist>\n\n"
        "Reason briefly (2-3 sentences max). Then output EXACTLY one of:\n"
        "  VERIFICATION: SUCCESS\n"
        "  VERIFICATION: FAIL: <one short sentence per issue, semicolon-separated>\n"
        "Output nothing after the VERIFICATION line."
    )
    try:
        raw = await llm_call_async(
            url=endpoint_url,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            headers=headers,
            temperature=0.0,
            max_tokens=600,
            timeout=60,
        )
    except Exception as exc:
        logger.warning("[agent] verifier subagent failed: %s", exc)
        return VerificationResult(
            VerificationStatus.UNKNOWN,
            detail=f"provider_error:{type(exc).__name__}",
        )
    result = _parse_verification(raw or "")
    if result.status is VerificationStatus.UNKNOWN:
        logger.warning("[agent] verifier returned an unparseable outcome: %s", result.detail)
    return result


async def run_verifier_subagent(
    instruction: str,
    actions_snapshot: str,
    *,
    endpoint_url: str,
    model: str,
    headers: dict,
) -> list[str]:
    """Legacy adapter used by agent_loop.

    PASS keeps the historical empty-list contract. FAIL returns the verifier's
    findings. UNKNOWN deliberately returns a finding as well, preventing a
    timeout, provider error, or malformed verifier response from being treated
    as implicit success by the current compatibility loop.
    """
    result = await verify_completion_subagent(
        instruction,
        actions_snapshot,
        endpoint_url=endpoint_url,
        model=model,
        headers=headers,
    )
    if result.status is VerificationStatus.PASS:
        return []
    if result.status is VerificationStatus.FAIL:
        return list(result.findings)
    return [_UNKNOWN_FINDING]


__all__ = [
    "EFFECTFUL_TOOLS",
    "MAX_VERIFIER_ROUNDS",
    "VerificationResult",
    "VerificationStatus",
    "build_actions_snapshot",
    "run_verifier_subagent",
    "verify_completion_subagent",
]
