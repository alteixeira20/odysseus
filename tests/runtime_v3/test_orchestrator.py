import os
import tempfile
import unittest

from src.agent.contracts import AgentRunRequest
from src.agent.runtime_v3 import ledger as ledger_module
from src.agent.runtime_v3.ledger import DurableRunLedger
from src.agent.runtime_v3.orchestrator import stream_with_durable_runtime


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ledger_module._LEDGER = DurableRunLedger(os.path.join(self.tmp.name, "r.sqlite3"))

    async def asyncTearDown(self):
        ledger_module._LEDGER.close()
        ledger_module._LEDGER = None
        self.tmp.cleanup()

    async def test_stream_without_terminal_is_durably_incomplete(self):
        request = AgentRunRequest.from_legacy_arguments(
            endpoint_url="http://local",
            model="m",
            messages=[{"role": "user", "content": "x"}],
            session_id="s1",
        )

        async def legacy():
            yield 'data: {"delta":"ok"}\n\n'

        events = [event async for event in stream_with_durable_runtime(request, legacy)]
        self.assertEqual(len(events), 1)
        row = ledger_module._LEDGER._conn.execute(
            "SELECT status,terminal_reason FROM agent_runs"
        ).fetchone()
        self.assertEqual(tuple(row), ("incomplete", "stream_ended_without_terminal"))


if __name__ == "__main__":
    unittest.main()
