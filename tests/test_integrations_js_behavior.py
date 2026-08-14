"""Behavioral regression tests for Settings > Integrations frontend code.

The Node harness executes the real ``integrationCategoryActions.js`` module
inside a deliberately small DOM shim. This keeps the test dependency-free
while still exercising production rendering, identity, layout, and modal
lifecycle behavior instead of reimplementing those rules in the test.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_JS_HELPER = _REPO / "tests" / "helpers" / "test_integrations.js"
_HAS_NODE = shutil.which("node") is not None


def _run_js_behavior_test():
    proc = subprocess.run(
        ["node", str(_JS_HELPER)],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, f"Node execution error:\n{proc.stderr}"
    results = json.loads(proc.stdout.strip())
    assert results, "Node integration harness returned no behavioral checks"
    failures = [result for result in results if result.get("pass") is not True]
    assert not failures, "Failed JS behavioral checks: " + json.dumps(failures, indent=2)


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_integrations_frontend_behavior():
    _run_js_behavior_test()
