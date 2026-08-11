"""Apply the narrow verifier truthfulness integration to agent_loop.py.

Temporary branch-only migration helper. Every edit is marker- or exact-match
asserted so it cannot silently rewrite a different loop revision.
"""

from pathlib import Path


PATH = Path("src/agent_loop.py")
text = PATH.read_text(encoding="utf-8")

old_import = '''from src.agent.supervision.verifier import (\n    EFFECTFUL_TOOLS as _VERIFIER_EFFECTFUL_TOOLS,\n    MAX_VERIFIER_ROUNDS as _VERIFIER_MAX_ROUNDS,\n    build_actions_snapshot as _build_actions_snapshot,\n    run_verifier_subagent as _run_verifier_subagent,\n)'''
new_import = '''from src.agent.supervision.verifier import (\n    EFFECTFUL_TOOLS as _VERIFIER_EFFECTFUL_TOOLS,\n    MAX_VERIFIER_ROUNDS as _VERIFIER_MAX_ROUNDS,\n    VerificationStatus as _VerificationStatus,\n    build_actions_snapshot as _build_actions_snapshot,\n    run_verifier_subagent as _run_verifier_subagent,\n)'''
if text.count(old_import) != 1:
    raise SystemExit(f"expected one verifier import block, found {text.count(old_import)}")
text = text.replace(old_import, new_import, 1)

old_state = '''    _effectful_used = False\n    _verifier_rounds = 0\n    _verifier_instruction = _extract_last_user_message(messages)'''
new_state = '''    _effectful_used = False\n    _verifier_rounds = 0\n    # Once a semantic verifier rejects completion, only fresh effectful work\n    # followed by a verifier PASS can clear the block. A prose-only retry must\n    # never convert a known verification failure into successful completion.\n    _verification_repair_required = False\n    _verifier_instruction = _extract_last_user_message(messages)'''
if text.count(old_state) != 1:
    raise SystemExit(f"expected one verifier state block, found {text.count(old_state)}")
text = text.replace(old_state, new_state, 1)

start_marker = "            # ── Completion verifier (mechanism 3a) ────────────────────\n"
end_marker = "            # ── Intent-without-action supervisor ─────────────────────\n"
if text.count(start_marker) != 1 or text.count(end_marker) != 1:
    raise SystemExit(
        f"verifier markers changed: start={text.count(start_marker)} end={text.count(end_marker)}"
    )
start = text.index(start_marker)
end = text.index(end_marker, start)

new_block = '''            # ── Completion verifier (mechanism 3a) ────────────────────\n            # Completion verification is an authority boundary for truthfulness:\n            # PASS permits completion, FAIL requires fresh effectful repair, and\n            # UNKNOWN is never interpreted as PASS. The verifier is opt-in, but\n            # when enabled its uncertainty must not create a false-success state.\n            _claimed_done = bool(_strip_think_blocks(cleaned_round).strip())\n            if (\n                _settings.verifier_enabled\n                and _verification_repair_required\n                and not _effectful_used\n                and _claimed_done\n            ):\n                _run_disposition = RunDisposition.INCOMPLETE\n                _run_disposition_reason = "verification_failed_without_repair"\n                logger.warning(\n                    "[agent] round %s attempted completion after verifier FAIL without fresh effectful repair",\n                    round_num,\n                )\n                break\n\n            if (\n                _settings.verifier_enabled\n                and _effectful_used\n                and not _force_answer\n                and _claimed_done\n            ):\n                if _verifier_rounds >= _VERIFIER_MAX_ROUNDS:\n                    _run_disposition = RunDisposition.INCOMPLETE\n                    _run_disposition_reason = "verification_retry_budget_exhausted"\n                    logger.warning(\n                        "[agent] verifier retry budget exhausted after %s rejected completion(s)",\n                        _verifier_rounds,\n                    )\n                    break\n\n                # Brief "working" indicator while the verifier runs.\n                yield f'data: {json.dumps({"type": "agent_step", "round": round_num})}\\n\\n'\n                _verification = await _run_verifier_subagent(\n                    _verifier_instruction,\n                    _build_actions_snapshot(tool_events),\n                    endpoint_url=endpoint_url, model=model, headers=headers,\n                )\n                if _verification.status is _VerificationStatus.UNKNOWN:\n                    _run_disposition = RunDisposition.INCOMPLETE\n                    _run_disposition_reason = "verification_inconclusive"\n                    logger.warning(\n                        "[agent] verifier inconclusive on round %s diagnostic=%s",\n                        round_num,\n                        _verification.diagnostic,\n                    )\n                    yield (\n                        "data: "\n                        + json.dumps({\n                            "type": "verification_inconclusive",\n                            "round": round_num,\n                            "diagnostic": _verification.diagnostic,\n                        })\n                        + "\\n\\n"\n                    )\n                    break\n\n                if _verification.status is _VerificationStatus.FAIL:\n                    _verifier_rounds += 1\n                    _verification_repair_required = True\n                    _vfail = list(_verification.findings)\n                    logger.info(\n                        "[agent] verifier flagged %s issue(s) on round %s: %s",\n                        len(_vfail),\n                        round_num,\n                        _vfail,\n                    )\n                    _note = "\\n\\n_Double-checked the work and found something to fix._\\n\\n"\n                    yield f'data: {json.dumps({"delta": _note})}\\n\\n'\n                    full_response += _note\n                    messages.append({\n                        "role": "system",\n                        "content": (\n                            "An independent verifier reviewed your work against the "\n                            "original request and found issues that must be fixed before "\n                            "this is actually done:\\n- " + "\\n- ".join(_vfail) +\n                            "\\n\\nFix these using the minimum necessary tools. Do not repeat "\n                            "already-committed side effects. Then finish only after the "\n                            "new evidence addresses every verifier finding."\n                        ),\n                    })\n                    # Fresh effectful work is required before another completion\n                    # claim can be verified; unchanged prose cannot clear FAIL.\n                    _effectful_used = False\n                    continue\n\n                # Only an explicit PASS clears a prior failure.\n                _verification_repair_required = False\n'''

text = text[:start] + new_block + text[end:]
PATH.write_text(text, encoding="utf-8")
print("Applied verifier truthfulness integration")
