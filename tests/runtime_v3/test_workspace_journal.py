import json
from pathlib import Path
import tempfile
import unittest

from src.agent.runtime_v3.workspace_journal import (
    SimulatedProcessCrash,
    WorkspaceJournalError,
    WorkspaceRecoveryRequired,
    WorkspaceTransactionJournal,
)


def snapshot(path: Path) -> dict:
    if not path.exists():
        return {"exists": False, "data": b"", "sha256": None, "mode": 0o644}
    data = path.read_bytes()
    import hashlib

    return {
        "exists": True,
        "data": data,
        "sha256": hashlib.sha256(data).hexdigest(),
        "mode": path.stat().st_mode & 0o777,
    }


def prepared(kind: str, path: Path, new: str = "") -> dict:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    return {
        "kind": kind,
        "path": str(path.resolve()),
        "old": old,
        "new": new,
        "snapshot": snapshot(path),
    }


class WorkspaceJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.journal_root = self.root / "journal"

    def tearDown(self):
        self.tmp.cleanup()

    def journal(self) -> WorkspaceTransactionJournal:
        return WorkspaceTransactionJournal(self.journal_root, lock_timeout_seconds=1)

    def test_normal_update_add_delete_commits_all_files(self):
        update = self.workspace / "update.txt"
        delete = self.workspace / "delete.txt"
        add = self.workspace / "add.txt"
        update.write_text("old update", encoding="utf-8")
        delete.write_text("old delete", encoding="utf-8")
        receipt = self.journal().commit(
            [
                prepared("update", update, "new update"),
                prepared("add", add, "new add"),
                prepared("delete", delete),
            ]
        )
        self.assertEqual(update.read_text(), "new update")
        self.assertEqual(add.read_text(), "new add")
        self.assertFalse(delete.exists())
        self.assertEqual(receipt.state, "committed")
        self.assertTrue(receipt.as_dict()["globally_atomic_after_recovery"])
        self.assertEqual(list((self.journal_root / "pending").glob("*.json")), [])

    def test_crash_after_first_replace_recovers_to_all_old(self):
        first = self.workspace / "first.txt"
        second = self.workspace / "second.txt"
        first.write_text("old first", encoding="utf-8")
        second.write_text("old second", encoding="utf-8")

        def crash(phase, index, _payload):
            if phase == "after_replace" and index == 0:
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.journal().commit(
                [
                    prepared("update", first, "new first"),
                    prepared("update", second, "new second"),
                ],
                fault_injector=crash,
            )
        self.assertEqual(first.read_text(), "new first")
        self.assertEqual(second.read_text(), "old second")

        receipts = self.journal().recover()
        self.assertEqual(first.read_text(), "old first")
        self.assertEqual(second.read_text(), "old second")
        self.assertEqual(receipts[0].state, "rolled_back")
        self.assertTrue(receipts[0].recovered)

    def test_crash_after_all_replacements_recovers_to_all_new(self):
        first = self.workspace / "first.txt"
        second = self.workspace / "second.txt"
        first.write_text("old first", encoding="utf-8")
        second.write_text("old second", encoding="utf-8")

        def crash(phase, _index, _payload):
            if phase == "after_all_replacements":
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.journal().commit(
                [
                    prepared("update", first, "new first"),
                    prepared("update", second, "new second"),
                ],
                fault_injector=crash,
            )
        receipts = self.journal().recover()
        self.assertEqual(first.read_text(), "new first")
        self.assertEqual(second.read_text(), "new second")
        self.assertEqual(receipts[0].state, "committed")

    def test_crash_during_payload_preparation_is_tracked_and_cleaned(self):
        first = self.workspace / "first.txt"
        second = self.workspace / "second.txt"
        first.write_text("old first", encoding="utf-8")
        second.write_text("old second", encoding="utf-8")

        def crash(phase, index, _payload):
            if phase == "after_payload" and index == 0:
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.journal().commit(
                [
                    prepared("update", first, "new first"),
                    prepared("update", second, "new second"),
                ],
                fault_injector=crash,
            )
        self.assertEqual(first.read_text(), "old first")
        self.assertEqual(second.read_text(), "old second")
        receipts = self.journal().recover()
        self.assertEqual(receipts[0].state, "rolled_back")
        self.assertEqual(list(self.workspace.glob(".*.ody-v3-*")), [])

    def test_concurrent_modification_before_commit_is_not_overwritten(self):
        first = self.workspace / "first.txt"
        first.write_text("old", encoding="utf-8")

        def modify(phase, _index, _payload):
            if phase == "after_payloads_ready":
                first.write_text("external edit", encoding="utf-8")

        with self.assertRaises(WorkspaceJournalError):
            self.journal().commit(
                [prepared("update", first, "new")],
                fault_injector=modify,
            )
        self.assertEqual(first.read_text(), "external edit")
        self.assertEqual(list((self.journal_root / "pending").glob("*.json")), [])

    def test_corrupt_journal_fails_closed(self):
        first = self.workspace / "first.txt"
        second = self.workspace / "second.txt"
        first.write_text("old first", encoding="utf-8")
        second.write_text("old second", encoding="utf-8")

        def crash(phase, index, _payload):
            if phase == "after_replace" and index == 0:
                raise SimulatedProcessCrash()

        with self.assertRaises(SimulatedProcessCrash):
            self.journal().commit(
                [
                    prepared("update", first, "new first"),
                    prepared("update", second, "new second"),
                ],
                fault_injector=crash,
            )
        manifest_path = next((self.journal_root / "pending").glob("*.json"))
        manifest = json.loads(manifest_path.read_text())
        manifest["phase"] = "committed"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(WorkspaceRecoveryRequired):
            self.journal().recover()
        self.assertEqual(first.read_text(), "new first")
        self.assertEqual(second.read_text(), "old second")


if __name__ == "__main__":
    unittest.main()
