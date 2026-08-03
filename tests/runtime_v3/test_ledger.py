import tempfile
from pathlib import Path
import unittest

from src.agent.runtime_v3.contracts import EffectClass, EffectStatus, RetryPolicy, RunStatus
from src.agent.runtime_v3.ledger import DurableRunLedger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.sqlite3"
        self.ledger = DurableRunLedger(self.path)
        self.ledger.create_run(
            run_id="r1", session_id="s1", owner="u1", workload="foreground",
            request={"message_count": 1}, limits={"max_rounds": 4},
            model="m", endpoint="http://local",
        )
        self.ledger.transition("r1", RunStatus.RUNNING)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_events_are_monotonic_and_replayable(self):
        self.assertEqual(self.ledger.append_event("r1", "delta", {"n": 1}), 1)
        self.assertEqual(self.ledger.append_event("r1", "delta", {"n": 2}), 2)
        self.assertEqual([e["seq"] for e in self.ledger.replay("r1")], [1, 2])

    def test_idempotency_replays_committed_result_without_execution(self):
        lease = self.ledger.begin_effect(
            run_id="r1", tool_name="send_email", arguments={"to": "a@example.com"},
            effect_class=EffectClass.EXTERNAL_WRITE, retry_policy=RetryPolicy.NEVER,
            idempotency_key="mail-1",
        )
        self.assertTrue(lease.should_execute)
        self.ledger.finish_effect(lease.effect_id, {"message_id": "42"})
        duplicate = self.ledger.begin_effect(
            run_id="r1", tool_name="send_email", arguments={"to": "a@example.com"},
            effect_class=EffectClass.EXTERNAL_WRITE, retry_policy=RetryPolicy.NEVER,
            idempotency_key="mail-1",
        )
        self.assertFalse(duplicate.should_execute)
        self.assertEqual(duplicate.status, EffectStatus.COMMITTED)
        self.assertEqual(duplicate.cached_result["message_id"], "42")

    def test_recovery_marks_started_effect_unknown(self):
        lease = self.ledger.begin_effect(
            run_id="r1", tool_name="external", arguments={"x": 1},
            effect_class=EffectClass.EXTERNAL_WRITE, retry_policy=RetryPolicy.NEVER,
        )
        self.ledger._conn.execute("UPDATE agent_runs SET process_id=-1 WHERE run_id='r1'")
        self.assertEqual(self.ledger.recover_interrupted(), ["r1"])
        row = self.ledger._conn.execute(
            "SELECT status FROM agent_effects WHERE effect_id=?", (lease.effect_id,)
        ).fetchone()
        self.assertEqual(row[0], EffectStatus.UNKNOWN.value)
        self.assertEqual(self.ledger.get_run("r1").status, RunStatus.INTERRUPTED)

    def test_illegal_terminal_transition_is_rejected(self):
        self.ledger.transition("r1", RunStatus.COMPLETED)
        with self.assertRaises(RuntimeError):
            self.ledger.transition("r1", RunStatus.RUNNING)


if __name__ == "__main__":
    unittest.main()
