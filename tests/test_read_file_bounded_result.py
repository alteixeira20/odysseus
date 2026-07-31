"""Structured truncation metadata for read_file (src/agent_tools/filesystem_tools.py),
part of the bounded-result contract in src/agent/execution/result_budget.py.
Previously a truncated read only said so in a human-readable notice embedded
in `output`; a caller now has `truncated`/`next_offset` fields to page
through the rest without re-reading from scratch.
"""

import json

import pytest

from src.agent_tools.filesystem_tools import ReadFileTool
from src.constants import MAX_READ_CHARS


async def _read(tmp_path, filename, body, **args):
    path = tmp_path / filename
    path.write_text(body, encoding="utf-8")
    payload = {"path": str(path), **args}
    return await ReadFileTool().execute(json.dumps(payload), {})


@pytest.mark.asyncio
async def test_small_whole_file_read_reports_not_truncated(tmp_path):
    result = await _read(tmp_path, "small.txt", "hello\nworld\n")
    assert result["exit_code"] == 0
    assert result["truncated"] is False
    assert "next_offset" not in result


@pytest.mark.asyncio
async def test_oversized_whole_file_read_reports_truncated_with_resume_line(tmp_path):
    # One line per char-budget-ish chunk so we can compute the expected
    # resume line deterministically.
    line = "x" * 100 + "\n"
    body = line * (MAX_READ_CHARS // len(line) + 20)
    result = await _read(tmp_path, "big.txt", body)

    assert result["exit_code"] == 0
    assert result["truncated"] is True
    assert "... [truncated at" in result["output"]
    assert result["next_offset"] > 0
    # The resume line must actually be a line boundary that was NOT fully
    # included (paging from there won't re-serve already-returned content).
    kept_lines = result["output"].split("\n... [truncated")[0].count("\n")
    assert result["next_offset"] == kept_lines + 1


@pytest.mark.asyncio
async def test_paged_read_within_limit_reports_no_next_offset_if_at_eof(tmp_path):
    result = await _read(tmp_path, "three.txt", "a\nb\nc\n", offset=1, limit=10)
    assert result["truncated"] is False
    assert "next_offset" not in result


@pytest.mark.asyncio
async def test_paged_read_hitting_limit_before_eof_reports_next_offset(tmp_path):
    body = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"  # 20 lines
    result = await _read(tmp_path, "twenty.txt", body, offset=1, limit=5)
    assert result["truncated"] is False  # limit reached deliberately, not a size cutoff
    assert result["next_offset"] == 6  # resume right after the 5 returned lines
    assert result["output"].count("\n") == 5


@pytest.mark.asyncio
async def test_next_offset_page_actually_continues_without_overlap(tmp_path):
    body = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
    first = await _read(tmp_path, "twenty.txt", body, offset=1, limit=5)
    second = await _read(
        tmp_path, "twenty.txt", body, offset=first["next_offset"], limit=5
    )
    assert "line1\n" in first["output"] and "line5\n" in first["output"]
    assert "line6\n" in second["output"]
    assert "line5" not in second["output"].split("\n")[0]


@pytest.mark.asyncio
async def test_paged_read_hitting_char_budget_reports_truncated_true(tmp_path):
    line = "y" * 200 + "\n"
    body = line * (MAX_READ_CHARS // len(line) + 50)
    result = await _read(tmp_path, "wide.txt", body, offset=1, limit=100_000)
    assert result["truncated"] is True
    assert result["next_offset"] > 0
