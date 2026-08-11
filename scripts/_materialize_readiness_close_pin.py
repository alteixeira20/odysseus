from __future__ import annotations

from pathlib import Path
import py_compile
import subprocess


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one transform anchor, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "scripts/agent_lab_readiness.py",
    '''    "tests/test_agent_verifier_truthfulness.py",\n    "tests/test_runtime_v2_execution_integrity.py",\n''',
    '''    "tests/test_agent_verifier_truthfulness.py",\n    "tests/test_tool_task_cancelled_on_disconnect.py",\n    "tests/test_runtime_v2_execution_integrity.py",\n''',
)
replace_once(
    "scripts/agent_lab_readiness.py",
    '''    "tests/test_tool_policy.py",\n    "tests/test_tool_registry.py",\n''',
    '''    "tests/test_tool_policy.py",\n    "tests/test_tool_registry.py",\n    "tests/test_tool_task_cancelled_on_disconnect.py",\n''',
)
replace_once(
    "tests/test_agent_lab_readiness.py",
    '''        "tests/test_agent_verifier_truthfulness.py",\n        "tests/test_runtime_v2_execution_integrity.py",\n''',
    '''        "tests/test_agent_verifier_truthfulness.py",\n        "tests/test_tool_task_cancelled_on_disconnect.py",\n        "tests/test_runtime_v2_execution_integrity.py",\n''',
)
replace_once(
    "tests/test_agent_lab_readiness.py",
    '''    assert "tests/test_tool_registry.py" in targets\n''',
    '''    assert "tests/test_tool_registry.py" in targets\n    assert "tests/test_tool_task_cancelled_on_disconnect.py" in targets\n''',
)

for path in ("scripts/agent_lab_readiness.py", "tests/test_agent_lab_readiness.py"):
    py_compile.compile(path, doraise=True)
subprocess.run(["git", "diff", "--check"], check=True)
print("readiness disconnect pin materialized successfully")
