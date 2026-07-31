"""Independent completion verification for effectful agent work."""

import logging

from src.agent.providers.adapters.default import strip_think_blocks


logger = logging.getLogger(__name__)

EFFECTFUL_TOOLS = {
    "create_document",
    "update_document",
    "edit_document",
    "bash",
    "python",
    "write_file",
}
MAX_VERIFIER_ROUNDS = 2


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


async def run_verifier_subagent(
    instruction: str,
    actions_snapshot: str,
    *,
    endpoint_url: str,
    model: str,
    headers: dict,
) -> list[str]:
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
        return []
    raw = strip_think_blocks(raw or "")
    last_verification = None
    for line in raw.splitlines():
        if "VERIFICATION:" in line:
            last_verification = line.strip()
    if (
        not last_verification
        or "VERIFICATION: FAIL:" not in last_verification
    ):
        return []
    reasons = last_verification.split(
        "VERIFICATION: FAIL:",
        1,
    )[1].strip()
    return [
        reason.strip()
        for reason in reasons.split(";")
        if reason.strip()
    ]
