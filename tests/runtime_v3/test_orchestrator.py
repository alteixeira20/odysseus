import json
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

    @staticmethod
    def _request(session_id="s1"):
        return AgentRunRequest.from_legacy_arguments(
            endpoint_url="http://local",
            model="m",
            messages=[{"role": "user", "content": "x"}],
            session_id=session_id,
        )

    def _durable_events(self):
        rows = ledger_module._LEDGER._conn.execute(
            "SELECT seq,event_type,payload_json FROM agent_events ORDER BY seq"
        ).fetchall()
        return [
            (int(row[0]), str(row[1]), json.loads(row[2]))
            for row in rows
        ]

    async def test_stream_without_terminal_is_durably_incomplete(self):
        request = self._request()

        async def legacy():
            yield 'data: {"delta":"ok"}\n\n'

        events = [event async for event in stream_with_durable_runtime(request, legacy)]
        self.assertEqual(len(events), 1)
        row = ledger_module._LEDGER._conn.execute(
            "SELECT status,terminal_reason FROM agent_runs"
        ).fetchone()
        self.assertEqual(tuple(row), ("incomplete", "stream_ended_without_terminal"))
        types = [item[1] for item in self._durable_events()]
        self.assertEqual(types, ["run_started", "stream_batch", "runtime_warning"])

    async def test_transient_stream_events_are_coalesced_without_changing_live_wire(self):
        request = self._request("batch")
        deltas = [
            f'data: {json.dumps({"delta": f"token-{index}"})}\n\n'
            for index in range(100)
        ]
        terminal = (
            'data: {"type":"run_state","state":"completed",'
            '"reason":"done","terminal":true,"resumable":false}\n\n'
        )

        async def legacy():
            for event in deltas:
                yield event
            yield terminal

        live = [event async for event in stream_with_durable_runtime(request, legacy)]
        self.assertEqual(live, deltas + [terminal])

        durable = self._durable_events()
        types = [item[1] for item in durable]
        self.assertEqual(types[0], "run_started")
        self.assertEqual(types[-1], "run_state")
        batches = [payload for _, kind, payload in durable if kind == "stream_batch"]
        self.assertGreaterEqual(len(batches), 2)
        self.assertLess(len(batches), len(deltas))
        replayed = [
            item["wire"]
            for batch in batches
            for item in batch["events"]
        ]
        self.assertEqual(replayed, deltas)

        row = ledger_module._LEDGER._conn.execute(
            "SELECT status,terminal_reason FROM agent_runs"
        ).fetchone()
        self.assertEqual(tuple(row), ("completed", "done"))

    async def test_critical_events_flush_transient_batch_and_preserve_durable_order(self):
        request = self._request("ordering")
        delta_one = 'data: {"delta":"one"}\n\n'
        tool_start = 'data: {"type":"tool_start","tool":"read_files"}\n\n'
        delta_two = 'data: {"delta":"two"}\n\n'
        terminal = (
            'data: {"type":"run_state","state":"completed",'
            '"reason":"done","terminal":true}\n\n'
        )

        async def legacy():
            yield delta_one
            yield tool_start
            yield delta_two
            yield terminal

        live = [event async for event in stream_with_durable_runtime(request, legacy)]
        self.assertEqual(live, [delta_one, tool_start, delta_two, terminal])
        durable = self._durable_events()
        self.assertEqual(
            [kind for _, kind, _ in durable],
            ["run_started", "stream_batch", "tool_start", "stream_batch", "run_state"],
        )
        self.assertEqual(durable[1][2]["events"][0]["wire"], delta_one)
        self.assertEqual(durable[3][2]["events"][0]["wire"], delta_two)

    async def test_unknown_event_types_are_never_buffered(self):
        request = self._request("future-event")
        unknown = 'data: {"type":"future_security_event","value":1}\n\n'
        terminal = (
            'data: {"type":"run_state","state":"completed",'
            '"reason":"done","terminal":true}\n\n'
        )

        async def legacy():
            yield unknown
            yield terminal

        _ = [event async for event in stream_with_durable_runtime(request, legacy)]
        durable = self._durable_events()
        self.assertEqual(
            [kind for _, kind, _ in durable],
            ["run_started", "future_security_event", "run_state"],
        )
        self.assertEqual(durable[1][2]["wire"], unknown)


if __name__ == "__main__":
    unittest.main()
