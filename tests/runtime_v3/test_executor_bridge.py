import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

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
    ToolResult,
    ToolResultStatus,
)
from src.agent.runtime_v3 import ledger as ledger_module
from src.agent.runtime_v3.executor_bridge import execute_with_durable_effects
from src.agent.runtime_v3.ledger import DurableRunLedger
from src.execution_policy import ExecutionMode


class ExecutorBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = str(Path(self.tmp.name).resolve())
        ledger_module._LEDGER = DurableRunLedger(os.path.join(root, "ledger.sqlite3"))
        self.context = AgentExecutionContext(
            run_id="run-executor",
            owner_id="owner",
            session_id="session",
            conversation_id="conversation",
            turn_id="turn",
            candidate_id="candidate",
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
            cancellation_token=CancellationToken("cancel"),
            tool_catalog_revision="catalog-r1",
            observation_ledger=ObservationLedger(),
            event_factory=object(),
        )
        self.call = NormalizedToolCall(
            call_id="call-executor",
            canonical_name="patch_workspace",
            arguments={"patch": "x"},
            provider_name="test",
            raw_name="patch_workspace",
            run_id="run-executor",
            conversation_id="conversation",
            turn_id="turn",
            candidate_id="candidate",
            authority_revision="authority-r1",
            workspace_revision="workspace-r1",
            tool_contract_revision="tools-r1",
        )
        self.effects = (
            Effect(
                kind="filesystem.write",
                target="x.txt",
                capability=Capability.WORKSPACE_WRITE,
                consequential=True,
            ),
        )

    async def asyncTearDown(self):
        ledger_module._LEDGER.close()
        ledger_module._LEDGER = None
        self.tmp.cleanup()

    async def test_duplicate_committed_call_does_not_reenter_handler(self):
        calls = 0

        async def implementation(*args, **kwargs):
            nonlocal calls
            calls += 1
            return ToolResult(
                call_id=self.call.call_id,
                canonical_name=self.call.canonical_name,
                status=ToolResultStatus.SUCCESS,
                data={"summary": "done"},
                attempted_effects=self.effects,
                observed_effects=self.effects,
                committed_effects=self.effects,
            )

        with patch(
            "src.agent.runtime_v3.executor_bridge._preflight_effects",
            return_value=self.effects,
        ):
            first = await execute_with_durable_effects(
                self.call, self.context, implementation=implementation
            )
            second = await execute_with_durable_effects(
                self.call, self.context, implementation=implementation
            )
        self.assertEqual(first.status, ToolResultStatus.SUCCESS)
        self.assertEqual(second.status, ToolResultStatus.SUCCESS)
        self.assertEqual(second.data["summary"], "done")
        self.assertEqual(calls, 1)

    async def test_ledger_failure_blocks_handler(self):
        calls = 0

        async def implementation(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise AssertionError("handler must not execute")

        with patch(
            "src.agent.runtime_v3.executor_bridge._preflight_effects",
            return_value=self.effects,
        ), patch(
            "src.agent.runtime_v3.executor_bridge.begin_tool_effect",
            side_effect=RuntimeError("disk unavailable"),
        ):
            result = await execute_with_durable_effects(
                self.call, self.context, implementation=implementation
            )
        self.assertEqual(calls, 0)
        self.assertEqual(result.status, ToolResultStatus.INCOMPLETE)
        self.assertEqual(result.error.code, "durable_effect_ledger_unavailable")


if __name__ == "__main__":
    unittest.main()
