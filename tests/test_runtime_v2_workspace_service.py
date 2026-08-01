"""Behavioral contracts for canonical coding workspace tools."""

import json
import os
from pathlib import Path
import shutil

import pytest

from src.agent.runtime_v2.workspace_service import (
    WORKSPACE_SERVICE,
    WorkspaceError,
    WorkspaceHashConflict,
    WorkspacePathError,
    WorkspaceRevisionConflict,
)


def _all_search_pages(root, arguments):
    collected = []
    request = dict(arguments)
    while True:
        page = WORKSPACE_SERVICE.search_text(str(root), request)
        collected.extend(page["matches"])
        continuation = page.get("continuation")
        if not continuation:
            return collected, page["backend"]
        request = {**arguments, "continuation": continuation["token"]}


def test_search_text_native_continuation_is_exact(tmp_path):
    for index in range(7):
        (tmp_path / f"match-{index}.txt").write_text(
            f"needle {index}\n",
            encoding="utf-8",
        )
    arguments = {
        "pattern": "needle",
        "fixed_string": True,
        "max_results": 2,
    }
    paged, backend = _all_search_pages(tmp_path, arguments)
    full = WORKSPACE_SERVICE.search_text(
        str(tmp_path),
        {**arguments, "max_results": 500},
    )["matches"]
    assert backend == "python_native"
    assert paged == full
    identities = [(item["path"], item["line"], item["column"]) for item in paged]
    assert len(identities) == len(set(identities)) == 7


def test_search_text_native_backend_preserves_context(tmp_path):
    (tmp_path / "sample.txt").write_text(
        "before\nneedle\nafter\n",
        encoding="utf-8",
    )
    result = WORKSPACE_SERVICE.search_text(
        str(tmp_path),
        {
            "pattern": "needle",
            "fixed_string": True,
            "context_lines": 1,
        },
    )
    assert result["backend"] == "python_native"
    assert [item["kind"] for item in result["matches"]] == [
        "context",
        "match",
        "context",
    ]


def test_read_files_has_one_budget_and_isolates_per_file_errors(tmp_path):
    (tmp_path / "large.txt").write_text(
        "first line\nsecond line\nthird line\n",
        encoding="utf-8",
    )
    result = WORKSPACE_SERVICE.read_files(
        str(tmp_path),
        {
            "requests": [
                {"path": "large.txt", "start_line": 1, "end_line": 3},
                {"path": "missing.txt"},
            ],
            "max_output_chars": 25,
        },
    )
    assert result["output_chars"] <= 25
    assert result["files"][0]["status"] == "success"
    assert result["files"][1]["status"] == "error"
    assert result["continuation"] is not None
    assert result["continuation"]["requests"][0]["path"] == "large.txt"


def test_patch_workspace_detects_stale_revision_and_file_hash(tmp_path):
    target = tmp_path / "source.txt"
    target.write_text("before\n", encoding="utf-8")
    revision = WORKSPACE_SERVICE.revision(str(tmp_path))
    digest = WORKSPACE_SERVICE.file_sha256(str(target))

    first = WORKSPACE_SERVICE.patch_workspace(
        str(tmp_path),
        {
            "expected_workspace_revision": revision,
            "operations": [
                {
                    "type": "replace",
                    "path": "source.txt",
                    "old": "before",
                    "new": "after",
                    "expected_sha256": digest,
                }
            ],
        },
    )
    assert first["workspace_revision"] != revision
    assert target.read_text(encoding="utf-8") == "after\n"

    with pytest.raises(WorkspaceRevisionConflict):
        WORKSPACE_SERVICE.patch_workspace(
            str(tmp_path),
            {
                "expected_workspace_revision": revision,
                "operations": [
                    {"type": "write", "path": "source.txt", "content": "stale"}
                ],
            },
        )
    with pytest.raises(WorkspaceHashConflict):
        WORKSPACE_SERVICE.patch_workspace(
            str(tmp_path),
            {
                "operations": [
                    {
                        "type": "write",
                        "path": "source.txt",
                        "content": "stale",
                        "expected_sha256": digest,
                    }
                ],
            },
        )


def test_failed_multi_file_commit_restores_every_original(tmp_path, monkeypatch):
    left = tmp_path / "left.txt"
    right = tmp_path / "right.txt"
    left.write_text("left-before", encoding="utf-8")
    right.write_text("right-before", encoding="utf-8")

    real_replace = os.replace
    calls = 0

    def fail_second_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second-file commit failure")
        return real_replace(source, destination)

    monkeypatch.setattr(
        "src.agent.runtime_v2.workspace_service.os.replace",
        fail_second_replace,
    )
    with pytest.raises(WorkspaceError, match="originals restored"):
        WORKSPACE_SERVICE.patch_workspace(
            str(tmp_path),
            {
                "operations": [
                    {"type": "write", "path": "left.txt", "content": "left-after"},
                    {"type": "write", "path": "right.txt", "content": "right-after"},
                ]
            },
        )
    assert left.read_text(encoding="utf-8") == "left-before"
    assert right.read_text(encoding="utf-8") == "right-before"


def test_prepared_crash_journal_is_recovered_before_next_revision(tmp_path):
    target = tmp_path / "interrupted.txt"
    target.write_text("before", encoding="utf-8")
    before_revision = WORKSPACE_SERVICE.revision(str(tmp_path))
    transaction_id = "prepared-crash"
    journal = Path(WORKSPACE_SERVICE.transaction_parent(str(tmp_path))) / transaction_id
    backups = journal / "backups"
    backups.mkdir(parents=True)
    (backups / "0.bin").write_text("before", encoding="utf-8")
    (journal / "manifest.json").write_text(
        json.dumps(
            {
                "version": 2,
                "transaction_id": transaction_id,
                "state": "prepared",
                "root": str(tmp_path.resolve()),
                "filesystem_identity": WORKSPACE_SERVICE._filesystem_identity(
                    str(tmp_path)
                ),
                "changes": [
                    {
                        "path": "interrupted.txt",
                        "existed": True,
                        "backup": "backups/0.bin",
                        "delete": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    target.write_text("partially committed", encoding="utf-8")

    recovered = WORKSPACE_SERVICE.recover_transactions(str(tmp_path))

    assert recovered == [
        {
            "transaction_id": transaction_id,
            "action": "restored",
            "state": "prepared",
        }
    ]
    assert target.read_text(encoding="utf-8") == "before"
    assert not journal.exists()
    assert WORKSPACE_SERVICE.revision(str(tmp_path)) != before_revision


def test_dry_run_produces_preview_without_mutation(tmp_path):
    target = tmp_path / "preview.txt"
    target.write_text("before", encoding="utf-8")
    result = WORKSPACE_SERVICE.patch_workspace(
        str(tmp_path),
        {
            "dry_run": True,
            "operations": [
                {"type": "replace", "path": "preview.txt", "old": "before", "new": "after"}
            ],
        },
    )
    assert result["transaction"] == "preview_only"
    assert target.read_text(encoding="utf-8") == "before"


def test_workspace_paths_are_canonical_and_confined(tmp_path):
    outside = tmp_path.parent / "outside-runtime-v2.txt"
    with pytest.raises(WorkspacePathError):
        WORKSPACE_SERVICE.resolve(str(tmp_path), str(outside))
