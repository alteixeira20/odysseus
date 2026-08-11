import tempfile
from pathlib import Path
import unittest

from src.agent.execution.observation_ledger import ObservationLedger
from src.agent.runtime_v2.contracts import (
    AgentExecutionContext,
    AuthorityGrant,
    CancellationToken,
    Capability,
    Effect,
    ExecutionRoot,
    ExecutionRootSource,
    NormalizedToolCall,
    RunBudgets,
    ToolError,
    ToolResult,
    ToolResultStatus,
)
from src.agent.runtime_v3.contracts import EffectClass, EffectStatus, RetryPolicy
from src.agent.runtime_v3.effect_bridge import (
    begin_tool_effect,
    duplicate_effect_result,
    finish_tool_effect,
    redact_sensitive,
)
from src.agent.runtime_v3.ledger import DurableRunLedger
from src.execution_policy import ExecutionMode


class EffectBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = str(Path(self.tmp.name).resolve())
        self.ledger = DurableRunLedger(Path(root) / "ledger.sqlite3")
        self.context = AgentExecutionContext(
            run_id="run-1",
            owner_id="owner-1",
            session_id="session-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            candidate_id="candidate-1",
            execution_mode=ExecutionMode.SANDBOXED,
            execution_root=ExecutionRoot(
                path=root,
                source=ExecutionRootSource.EPHEMERAL_WORKSPACE,
                writable=True,
                workspace_revision="workspace-r1",
            ),
            authority_grant=AuthorityGrant(
                capabilities=frozenset(Capability),
                revision="authority-r1",
            ),
            budgets=RunBudgets(),
            cancellation_token=CancellationToken("cancel-1"),
            tool_catalog_revision="catalog-r1",
            observation_ledger=ObservationLedger(),
            event_factory=object(),
        )
        self.call = NormalizedToolCall(
            call_id="call-1",
            canonical_name="patch_workspace",
            arguments={"patch": "safe", "api_key": "must-not-persist"},
            provider_name="test",
            raw_name="patch_workspace",
            run_id="run-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            candidate_id="candidate-1",
            authority_revision="authority-r1",
            workspace_revision="workspace-r1",
            tool_contract_revision="tools-r1",
        )
        self.effects = (
            Effect(
                kind="filesystem.write",
                target="file.txt",
                capability=Capability.WORKSPACE_WRITE,
                consequential=True,
            ),
        )

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_sensitive_arguments_are_fingerprinted(self):
        value = redact_sensitive({"api_key": "secret", "nested": {"password": "p"}})
        self.assertNotIn("secret", str(value))
        self.assertNotIn("'p'", str(value))
        self.assertIn("$redacted_sha256", str(value))

    def test_unmodelled_effect_contract_is_never_retryable(self):
        call = NormalizedToolCall(
            **{
                **self.call.__dict__,
                "call_id": "unknown-1",
                "canonical_name": "future_tool",
                "raw_name": "future_tool",
            }
        )
        handle = begin_tool_effect(call, self.context, (), ledger=self.ledger)
        self.assertTrue(handle.should_execute)
        self.assertEqual(handle.effect_class, EffectClass.UNKNOWN)
        self.assertEqual(handle.retry_policy, RetryPolicy.NEVER)

        finish_tool_effect(
            handle,
            ToolResult(
                call_id="unknown-1",
                canonical_name="future_tool",
                status=ToolResultStatus.ERROR,
                error=ToolError("unknown_failure", "outcome cannot be proven"),
            ),
        )
        row = self.ledger._conn.execute(
            "SELECT status,retry_policy FROM agent_effects WHERE effect_id=?",
            (handle.effect_id,),
        ).fetchone()
        self.assertEqual(tuple(row), (EffectStatus.UNKNOWN.value, RetryPolicy.NEVER.value))

        duplicate = begin_tool_effect(call, self.context, (), ledger=self.ledger)
        self.assertFalse(duplicate.should_execute)
        blocked = duplicate_effect_result(call, duplicate)
        self.assertEqual(blocked.status, ToolResultStatus.INCOMPLETE)
        self.assertTrue(blocked.data["reconciliation_required"])

    def test_committed_duplicate_replays_without_execution(self):
        handle = begin_tool_effect(self.call, self.context, self.effects, ledger=self.ledger)
        self.assertTrue(handle.should_execute)
        result = ToolResult(
            call_id="call-1",
            canonical_name="patch_workspace",
            status=ToolResultStatus.SUCCESS,
            data={"summary": "patched"},
            attempted_effects=self.effects,
            observed_effects=self.effects,
            committed_effects=self.effects,
        )
        finish_tool_effect(handle, result)

        duplicate = begin_tool_effect(self.call, self.context, self.effects, ledger=self.ledger)
        self.assertFalse(duplicate.should_execute)
        replay = duplicate_effect_result(self.call, duplicate)
        self.assertEqual(replay.status, ToolResultStatus.SUCCESS)
        self.assertEqual(replay.data["summary"], "patched")

    def test_separate_approval_scopes_do_not_poison_each_other(self):
        invalid_attempt = begin_tool_effect(
            self.call,
            self.context,
            self.effects,
            ledger=self.ledger,
            idempotency_scope="approval-invalid",
        )
        self.ledger.fail_effect(invalid_attempt.effect_id, {"code": "approval_invalid"})
        valid_attempt = begin_tool_effect(
            self.call,
            self.context,
            self.effects,
            ledger=self.ledger,
            idempotency_scope="approval-valid",
        )
        self.assertTrue(valid_attempt.should_execute)
        self.assertNotEqual(valid_attempt.effect_id, invalid_attempt.effect_id)
        repeated_invalid = begin_tool_effect(
            self.call,
            self.context,
            self.effects,
            ledger=self.ledger,
            idempotency_scope="approval-invalid",
        )
        self.assertFalse(repeated_invalid.should_execute)
        blocked = duplicate_effect_result(self.call, repeated_invalid)
        self.assertEqual(blocked.status, ToolResultStatus.INCOMPLETE)
        self.assertTrue(blocked.data["reconciliation_required"])

    def test_ambiguous_external_failure_is_unknown_and_not_retried(self):
        external = (
            Effect(
                kind="email.send",
                target="message",
                capability=Capability.EXTERNAL_WRITE,
                consequential=True,
                opaque=True,
            ),
        )
        call = NormalizedToolCall(
            **{**self.call.__dict__, "call_id": "mail-1", "canonical_name": "send_email", "raw_name": "send_email"}
        )
        handle = begin_tool_effect(call, self.context, external, ledger=self.ledger)
        finish_tool_effect(
            handle,
            ToolResult(
                call_id="mail-1",
                canonical_name="send_email",
                status=ToolResultStatus.ERROR,
                error=ToolError("transport_lost", "connection closed after send"),
                attempted_effects=external,
                observed_effects=external,
                unknown_effects=external,
            ),
        )
        row = self.ledger._conn.execute(
            "SELECT status FROM agent_effects WHERE effect_id=?", (handle.effect_id,)
        ).fetchone()
        self.assertEqual(row[0], EffectStatus.UNKNOWN.value)
        duplicate = begin_tool_effect(call, self.context, external, ledger=self.ledger)
        blocked = duplicate_effect_result(call, duplicate)
        self.assertFalse(duplicate.should_execute)
        self.assertEqual(blocked.status, ToolResultStatus.INCOMPLETE)
        self.assertTrue(blocked.data["reconciliation_required"])


if __name__ == "__main__":
    unittest.main()
