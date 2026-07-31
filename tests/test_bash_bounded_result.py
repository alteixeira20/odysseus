"""Structured truncation metadata on BashTool results (src/agent_tools/
subprocess_tools.py) — part of the bounded-result contract in
src/agent/execution/result_budget.py.
"""

import pytest

from src.agent_tools.subprocess_tools import BashTool
from src.constants import MAX_OUTPUT_CHARS


@pytest.mark.asyncio
async def test_small_output_reports_not_truncated():
    result = await BashTool().execute("echo hi", {})
    assert result["exit_code"] == 0
    assert result["stdout_truncated"] is False
    assert result["stderr_truncated"] is False
    assert result["stdout_total_chars"] == len("hi\n")


@pytest.mark.asyncio
async def test_oversized_stdout_reports_truncated_with_total_chars():
    n_chars = MAX_OUTPUT_CHARS + 5000
    cmd = f"python3 -c \"print('x' * {n_chars})\""
    result = await BashTool().execute(cmd, {})
    assert result["exit_code"] == 0
    assert result["stdout_truncated"] is True
    assert result["stdout_total_chars"] >= n_chars
    assert "..." in result["stdout"] or "truncat" in result["stdout"].lower()
