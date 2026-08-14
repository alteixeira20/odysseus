"""Frontend JS behavioral tests for Integrations settings module (settings.js).

Tests category filtering, CLI agent rendering (AGY, Codex, Claude), token ID identity,
duplicate display names, HTML escaping, and separation of unrelated API tokens.
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
    assert proc.returncode == 0, f"Node execution error: {proc.stderr}"
    results = json.loads(proc.stdout.strip())
    for r in results:
        assert r["pass"] is True, f"Failed JS behavioral test: {r['test']}"


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_integrations_frontend_behavior():
    _run_js_behavior_test()
