"""Independent completion verification for effectful agent work."""

from dataclasses import dataclass
from enum import Enum
import logging

from src.agent.providers.adapters.default import strip_think_blocks


logger = logging.getLogger(__name__)

# Compatibility names plus the canonical Runtime V2/V3 names that can create
# externally observable or workspace effects. Argument-aware effect resolution
# remains authoritative for execution; this set only decides whether an
# opt-in semantic completion check is warranted after a successful tool round.
EFFECTFUL_TOOLS = {
    "create_document",
    "update_document",
    "edit_document",
    "patch_workspace",
    "run_sandbox_command",
    "run_host_command",
    "run_python",
    "bash",
    "python",
    "write_file",
    "edit_file",
    "replace_file",
    "append_file",
    "send_email",
    "reply_to_email",
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
    diagnostic: str = ""

    @classmethod
    def passed(cls) -> "VerificationResult":
        return cls(VerificationStatus.PASS)

    @classmethod
    def failed(cls, findings: list[str] | tuple[str, ...]) -> "VerificationResult":
        cleaned = tuple(str(item).strip() for item in findings if str(item).strip())
        return cls(VerificationStatus.FAIL, findings=cleaned)

    @classmethod
    def unknown(cls, diagnostic: str) -> "VerificationResult":
        return cls(VerificationStatus.UNKNOWN, diagnostic=str(diagnostic or "")[:500])


def build_actions_snapshot(tool_events: list, limit: int = 8000) -> str:
    parts = []
    for event in tool_events:
        tool = event.get("tool", "?")
        command = (event.get("command") or "").strip()
        output = (event.get("output") or "").strip()
        exit_code = event.get("exit_code")
        head = f"[{tool}] {command}" if command else f"[{tool}]"
        exit_text = f" (exit {exit_code})" if exit_code not in (None, 0) else ""
        body = output[:1200] + " …" if len(output) > 1200 else output or "(no output)"
        parts.append(f"{head}{exit_text}\n-> {body}")
    snapshot = "\n\n".join(parts)
    return snapshot[:limit] if len(snapshot) > limit else snapshot


def parse_verification_response(raw: str) -> VerificationResult:
    """Parse the verifier protocol without interpreting absence as success."""

    cleaned = strip_think_blocks(raw or "")
    verification_lines = [
        line.strip()
        for line in cleaned.splitlines()
        if line.strip().startswith("VERIFICATION:")
    ]
    if not verification_lines:
        return VerificationResult.unknown("missing_verification_marker")

    line = verification_lines[-1]
    if line == "VERIFICATION: SUCCESS":
        return VerificationResult.passed()
    prefix = "VERIFICATION: FAIL:"
    if line.startswith(prefix):
        reasons = [
            reason.strip()
            for reason in line[len(prefix):].strip().split(";")
            if reason.strip()
        ]
        if reasons:
            return VerificationResult.failed(reasons)
        return VerificationResult.unknown("empty_failure_reason")
    return VerificationResult.unknown("malformed_verification_marker")


async def run_verifier_subagent(
    instruction: str,
    actions_snapshot: str,
    *,
    endpoint_url: str,
    model: str,
    headers: dict,
) -> VerificationResult:
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
        return VerificationResult.unknown(f"verifier_call_failed:{type(exc).__name__}")
    return parse_verification_response(raw or "")
