"""Apply the force-answer verifier audit fix with exact assertions."""
from pathlib import Path

path = Path("src/agent_loop.py")
text = path.read_text(encoding="utf-8")

old = '''            if (\n                _settings.verifier_enabled\n                and _effectful_used\n                and not _force_answer\n                and _claimed_done\n            ):'''
new = '''            if (\n                _settings.verifier_enabled\n                and _effectful_used\n                and _claimed_done\n            ):'''
if text.count(old) != 1:
    raise SystemExit(f"expected verifier eligibility block once, found {text.count(old)}")
text = text.replace(old, new, 1)

old_fail = '''                if _verification.status is _VerificationStatus.FAIL:\n                    _verifier_rounds += 1\n                    _verification_repair_required = True\n                    _vfail = list(_verification.findings)'''
new_fail = '''                if _verification.status is _VerificationStatus.FAIL:\n                    _verifier_rounds += 1\n                    _verification_repair_required = True\n                    _vfail = list(_verification.findings)\n                    if _force_answer:\n                        _run_disposition = RunDisposition.INCOMPLETE\n                        _run_disposition_reason = "verification_failed_at_force_answer"\n                        logger.warning(\n                            "[agent] verifier rejected force-answer completion on round %s: %s",\n                            round_num,\n                            _vfail,\n                        )\n                        break'''
if text.count(old_fail) != 1:
    raise SystemExit(f"expected verifier FAIL block once, found {text.count(old_fail)}")
text = text.replace(old_fail, new_fail, 1)

path.write_text(text, encoding="utf-8")
print("Applied force-answer verifier audit fix")
