import json
import tempfile
from pathlib import Path
import unittest

from src.agent.runtime_v3.ledger import DurableRunLedger
from src.agent.runtime_v3.stream_journal import DurableStreamJournal


class DurableStreamJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = DurableRunLedger(Path(self.tmp.name) / "ledger.sqlite3")
        self.ledger.create_run(
            run_id="run",
            session_id="session",
            owner="owner",
            workload="test",
            request={"test": True},
            limits={},
            model="m",
            endpoint="http://local",
        )

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def _rows(self):
        return self.ledger._conn.execute(
            "SELECT event_type,payload_json FROM agent_events ORDER BY seq"
        ).fetchall()

    def test_transient_events_batch_at_count_limit(self):
        journal = DurableStreamJournal(
            self.ledger,
            "run",
            max_event_bytes=64 * 1024,
            max_batch_events=3,
            max_batch_bytes=32 * 1024,
        )
        for index in range(3):
            journal.record("delta", f'data: {{"delta":"{index}"}}\n\n')

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "stream_batch")
        payload = json.loads(rows[0][1])
        self.assertEqual(len(payload["events"]), 3)
        self.assertEqual(journal.pending_count, 0)

    def test_critical_and_unknown_types_flush_before_immediate_append(self):
        journal = DurableStreamJournal(
            self.ledger,
            "run",
            max_event_bytes=64 * 1024,
            max_batch_events=32,
            max_batch_bytes=32 * 1024,
        )
        journal.record("delta", 'data: {"delta":"a"}\n\n')
        journal.record("tool_result", 'data: {"type":"tool_result"}\n\n')
        journal.record("future_runtime_boundary", 'data: {"type":"future_runtime_boundary"}\n\n')

        rows = self._rows()
        self.assertEqual(
            [row[0] for row in rows],
            ["stream_batch", "tool_result", "future_runtime_boundary"],
        )

    def test_large_transient_event_falls_back_to_single_event(self):
        journal = DurableStreamJournal(
            self.ledger,
            "run",
            max_event_bytes=16 * 1024,
            max_batch_events=32,
            max_batch_bytes=1024,
        )
        wire = "data: " + json.dumps({"delta": "x" * 2000}) + "\n\n"
        journal.record("delta", wire)

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "delta")
        self.assertEqual(json.loads(rows[0][1])["wire"], wire)


if __name__ == "__main__":
    unittest.main()
