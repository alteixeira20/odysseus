"""Production-backed cursor contracts for repository navigation tools."""

import json

import pytest

from src.agent_tools.filesystem_tools import GlobTool, GrepTool, LsTool
from src.constants import MAX_OUTPUT_CHARS
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS


def _payload(**kwargs):
    return json.dumps(kwargs)


@pytest.mark.asyncio
async def test_ls_pages_without_overlap_and_reports_resume_cursor(tmp_path):
    for index in range(7):
        (tmp_path / f"file-{index}.txt").write_text(str(index), encoding="utf-8")

    first = await LsTool().execute(
        _payload(path=str(tmp_path), max_results=3), {}
    )
    second = await LsTool().execute(
        _payload(path=str(tmp_path), max_results=3, offset=first["next_offset"]),
        {},
    )

    assert first["truncated"] is True
    assert first["next_offset"] == 3
    assert first["returned_results"] == 3
    assert second["offset"] == 3
    first_entries = {
        line for line in first["output"].splitlines() if line.startswith("  file-")
    }
    second_entries = {
        line for line in second["output"].splitlines() if line.startswith("  file-")
    }
    assert first_entries.isdisjoint(second_entries)
    assert "offset=3" in first["resume_hint"]


@pytest.mark.asyncio
async def test_glob_pages_without_repeating_paths(tmp_path):
    for index in range(6):
        (tmp_path / f"module-{index}.py").write_text("pass\n", encoding="utf-8")

    first = await GlobTool().execute(
        _payload(path=str(tmp_path), pattern="*.py", max_results=2), {}
    )
    second = await GlobTool().execute(
        _payload(
            path=str(tmp_path),
            pattern="*.py",
            max_results=2,
            offset=first["next_offset"],
        ),
        {},
    )

    first_paths = {line for line in first["output"].splitlines() if line.startswith("/")}
    second_paths = {line for line in second["output"].splitlines() if line.startswith("/")}
    assert first["truncated"] is True
    assert first_paths
    assert first_paths.isdisjoint(second_paths)


@pytest.mark.asyncio
async def test_grep_pages_without_repeating_matches(tmp_path):
    target = tmp_path / "many.txt"
    target.write_text("\n".join(f"needle {index}" for index in range(8)), encoding="utf-8")

    first = await GrepTool().execute(
        _payload(path=str(tmp_path), pattern="needle", max_results=3), {}
    )
    second = await GrepTool().execute(
        _payload(
            path=str(tmp_path),
            pattern="needle",
            max_results=3,
            offset=first["next_offset"],
        ),
        {},
    )

    first_hits = {line for line in first["output"].splitlines() if ":needle " in line}
    second_hits = {line for line in second["output"].splitlines() if ":needle " in line}
    assert first["next_offset"] == 3
    assert first_hits.isdisjoint(second_hits)
    assert second["returned_results"] == 3


@pytest.mark.asyncio
async def test_large_grep_result_is_bounded_and_cursor_resumes_after_visible_page(
    tmp_path,
):
    target = tmp_path / "wide.txt"
    target.write_text(
        "\n".join(f"needle {index} " + "x" * 500 for index in range(100)),
        encoding="utf-8",
    )

    first = await GrepTool().execute(
        _payload(path=str(tmp_path), pattern="needle", max_results=200), {}
    )
    second = await GrepTool().execute(
        _payload(
            path=str(tmp_path),
            pattern="needle",
            max_results=200,
            offset=first["next_offset"],
        ),
        {},
    )

    assert len(first["output"]) <= MAX_OUTPUT_CHARS
    assert first["truncated"] is True
    assert 0 < first["returned_results"] < 100
    assert first["next_offset"] == first["returned_results"]
    assert f":{first['next_offset'] + 1}:needle" in second["output"]


def test_provider_schemas_advertise_repository_resume_arguments():
    schemas = {
        item["function"]["name"]: item["function"]["parameters"]["properties"]
        for item in FUNCTION_TOOL_SCHEMAS
    }
    for name in ("grep", "glob", "ls"):
        assert "offset" in schemas[name]
        assert schemas[name]["max_results"]["maximum"] == 200
