import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from src.agent.runtime_v3.contracts import EffectClass, EffectStatus, RetryPolicy, RunStatus
from src.agent.runtime_v3.ledger import DurableRunLedger
from src.agent.runtime_v3.operations import RuntimeAccessDenied, RuntimeConflict, RuntimeOperations


class RuntimeOperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = DurableRunLedger(Path(self.tmp.name) / "runtime.sqlite3")
        self.ledger.create_run(
            run_id="owned-run",
            session_id="session-1",
            owner="alice",
            workload="foreground",
            request={"message_count": 1},
            limits={"max_rounds": 50},
            model="model-a",
            endpoint="https://api.openai.com/v1/chat/completions?api_key=hidden",
        )
        self.ledger.transition("owned-run", RunStatus.RUNNING)
        self.operations = RuntimeOperations(self.ledger)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_owner_scoping_hides_other_users_runs(self):
        self.assertEqual(len(self.operations.list_runs(owner="alice")), 1)
        self.assertEqual(self.operations.list_runs(owner="bob"), [])
        with self.assertRaises(RuntimeAccessDenied):
            self.operations.get_run("owned-run", owner="bob")

    def test_endpoint_summary_does_not_expose_url_or_query_secret(self):
        run = self.operations.get_run("owned-run", owner="alice")
        text = str(run["endpoint"])
        self.assertIn("openai", text)
        self.assertNotIn("api_key", text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("chat/completions", text)

    def test_durable_event_replay_is_owner_scoped(self):
        self.ledger.append_event("owned-run", "delta", {"text": "hello"})
        events = self.operations.events("owned-run", owner="alice")
        self.assertEqual(events[0]["seq"], 1)
        with self.assertRaises(RuntimeAccessDenied):
            self.operations.events("owned-run", owner="bob")

    def test_cancel_without_active_worker_records_command_and_terminal_state(self):
        with patch("src.agent_runs.stop", return_value=False):
            command = self.operations.request_cancel(
                "owned-run", owner="alice", reason="operator requested"
            )
        self.assertFalse(command["active_task_cancelled"])
        self.assertEqual(self.ledger.get_run("owned-run").status, RunStatus.CANCELLED)
        detail = self.operations.get_run("owned-run", owner="alice")
        self.assertEqual(detail["commands"][0]["type"], "cancel")
        self.assertEqual(detail["commands"][0]["status"], "completed")
        with self.assertRaises(RuntimeConflict):
            self.operations.request_cancel("owned-run", owner="alice", reason="again")

    def test_unknown_effect_reconciliation_requires_revision_and_note(self):
        lease = self.ledger.begin_effect(
            run_id="owned-run",
            tool_name="send_email",
            arguments={"to": "example@example.com"},
            effect_class=EffectClass.EXTERNAL_WRITE,
            retry_policy=RetryPolicy.NEVER,
            idempotency_key="mail-1",
        )
        self.ledger.mark_effect_unknown(lease.effect_id)
        detail = self.operations.get_run("owned-run", owner="alice")
        effect = detail["effects"][0]
        with self.assertRaises(ValueError):
            self.operations.reconcile_effect(
                "owned-run",
                lease.effect_id,
                owner="alice",
                outcome="committed",
                note="short",
                expected_revision=effect["revision"],
            )
        result = self.operations.reconcile_effect(
            "owned-run",
            lease.effect_id,
            owner="alice",
            outcome="committed",
            note="Verified in the provider delivery log.",
            expected_revision=effect["revision"],
            evidence={"provider_message_id": "42"},
        )
        self.assertEqual(result["status"], EffectStatus.COMMITTED.value)
        with self.assertRaises(RuntimeConflict):
            self.operations.reconcile_effect(
                "owned-run",
                lease.effect_id,
                owner="alice",
                outcome="failed",
                note="A second stale reconciliation attempt.",
                expected_revision=effect["revision"],
            )


if __name__ == "__main__":
    unittest.main()
