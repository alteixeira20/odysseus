"""Atomicity, optimistic concurrency, and stable repository paging contracts."""

import hashlib
import json
import os

import pytest

from src.agent_tools import filesystem_tools
from src.agent_tools.filesystem_tools import ApplyPatchTool, GlobTool, WriteFileTool
from src.agent.runtime_v3.workspace_journal import WorkspaceTransactionJournal


@pytest.mark.asyncio
async def test_multi_file_patch_failure_restores_every_original(tmp_path, monkeypatch):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first-old\n", encoding="utf-8")
    second.write_text("second-old\n", encoding="utf-8")
    patch = f"""*** Begin Patch
*** Update File: {first}
@@
-first-old
+first-new
*** Update File: {second}
@@
-second-old
+second-new
*** End Patch"""

    real_apply_new = WorkspaceTransactionJournal._apply_new
    injected = {"done": False}

    def fail_second_visible_commit(self, operation):
        if operation["path"] == str(second) and not injected["done"]:
            injected["done"] = True
            raise OSError("injected second-file commit failure")
        return real_apply_new(self, operation)

    # Fault-inject the journal's actual visible commit boundary. The old test
    # patched filesystem_tools._replace_staged, but Runtime V3 now owns multi-
    # file visibility and rollback below that legacy helper.
    monkeypatch.setattr(
        WorkspaceTransactionJournal,
        "_apply_new",
        fail_second_visible_commit,
    )
    result = await ApplyPatchTool().execute(json.dumps({"patch_text": patch}), {})

    assert result["exit_code"] == 1
    assert "injected second-file commit failure" in result["error"]
    assert first.read_text(encoding="utf-8") == "first-old\n"
    assert second.read_text(encoding="utf-8") == "second-old\n"
    assert not list(tmp_path.glob(".*.ody-*"))


@pytest.mark.asyncio
async def test_write_file_rejects_stale_expected_hash(tmp_path):
    target = tmp_path / "state.txt"
    target.write_text("current", encoding="utf-8")
    current_hash = hashlib.sha256(b"current").hexdigest()

    stale = await WriteFileTool().execute(
        json.dumps(
            {
                "path": str(target),
                "content": "overwritten",
                "expected_sha256": "0" * 64,
            }
        ),
        {},
    )
    assert stale["exit_code"] == 1
    assert "content changed" in stale["error"]
    assert target.read_text(encoding="utf-8") == "current"

    committed = await WriteFileTool().execute(
        json.dumps(
            {
                "path": str(target),
                "content": "updated",
                "expected_sha256": current_hash,
            }
        ),
        {},
    )
    assert committed["exit_code"] == 0
    assert committed["previous_sha256"] == current_hash
    assert committed["sha256"] == hashlib.sha256(b"updated").hexdigest()
    assert target.read_text(encoding="utf-8") == "updated"


@pytest.mark.asyncio
async def test_glob_offset_pages_follow_one_deterministic_traversal(tmp_path, monkeypatch):
    # Shrink the scan/page bound so the old "sort each growing partial scan by
    # mtime" defect is reproduced with a small tree. The late z/ file has the
    # highest mtime and would be inserted before already-issued offsets.
    monkeypatch.setattr(filesystem_tools, "_CODENAV_MAX_HITS", 2)
    early = tmp_path / "a"
    late = tmp_path / "z"
    early.mkdir()
    late.mkdir()
    expected = set()
    for index in range(12):
        path = early / f"module-{index:02}.py"
        path.write_text("pass\n", encoding="utf-8")
        os.utime(path, (1, 1))
        expected.add(str(path))
    late_path = late / "newest.py"
    late_path.write_text("pass\n", encoding="utf-8")
    os.utime(late_path, (10_000, 10_000))
    expected.add(str(late_path))

    seen = []
    offset = 0
    for _ in range(20):
        result = await GlobTool().execute(
            json.dumps(
                {
                    "path": str(tmp_path),
                    "pattern": "**/*.py",
                    "max_results": 2,
                    "offset": offset,
                }
            ),
            {},
        )
        seen.extend(
            line for line in result["output"].splitlines()
            if line.startswith("/")
        )
        if "next_offset" not in result:
            break
        offset = result["next_offset"]

    assert len(seen) == len(set(seen))
    assert set(seen) == expected
