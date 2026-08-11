import os
import tempfile
import unittest

from src.agent.contracts import AgentRunRequest, RunDisposition
from src.agent.events import run_state_event
from src.agent.runtime_v3.contracts import RunStatus
from src.agent.runtime_v3.ledger import DurableRunLedger
from src.agent.runtime_v3.lifecycle import DurableRunLifecycle


class DurableLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = DurableRunLedger(os.path.join(self.tmp.name, "runtime.sqlite3"))

    async def asyncTearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    @staticmethod
    def _request(session_id="session-a"):
        return AgentRunRequest.from_legacy_arguments(
            endpoint_url="http://local",
            model="model-a",
            messages=[{"role": "user", "content": "hello"}],
            session_id=session_id,
        )

    def _run_row(self):
        return self.ledger._conn.execute(
            "SELECT status,terminal_reason,resumable FROM agent_runs"
        ).fetchone()

    async def test_typed_terminal_wins_over_wrong_legacy_parser(self):
        parser_calls = []

        def wrong_parser(wire):
            parser_calls.append(wire)
            return RunStatus.FAILED, "wrong-parser-result", False

        lifecycle = DurableRunLifecycle(
            ledger=self.ledger,
            legacy_terminal_parser=wrong_parser,
        )

        async def backend():
            yield run_state_event(
                RunDisposition.COMPLETED,
                reason="typed-completion",
            )

        events = [
            event
            async for event in lifecycle.stream(self._request(), backend)
        ]

        self.assertEqual(len(events), 1)
        self.assertEqual(tuple(self._run_row()), ("completed", "typed-completion", 0))
        self.assertEqual(parser_calls, [])

    async def test_legacy_run_state_wire_remains_compatible_fallback(self):
        lifecycle = DurableRunLifecycle(ledger=self.ledger)

        async def backend():
            # Deliberately bypass run_state_event()/AgentEvent observation.
            yield (
                'data: {"type":"run_state","state":"incomplete",'
                '"terminal":true,"reason":"legacy-only","resumable":true}\n\n'
            )

        events = [
            event
            async for event in lifecycle.stream(self._request(), backend)
        ]

        self.assertEqual(len(events), 1)
        self.assertEqual(tuple(self._run_row()), ("incomplete", "legacy-only", 1))

    async def test_typed_nonterminal_does_not_hide_missing_terminal(self):
        lifecycle = DurableRunLifecycle(ledger=self.ledger)

        async def backend():
            from src.agent.events import AgentEvent, encode_legacy_sse

            yield encode_legacy_sse(
                AgentEvent.typed(
                    "run_status",
                    phase="working",
                    label="Working",
                    ephemeral=True,
                )
            )

        events = [
            event
            async for event in lifecycle.stream(self._request(), backend)
        ]

        self.assertEqual(len(events), 1)
        self.assertEqual(
            tuple(self._run_row()),
            ("incomplete", "stream_ended_without_terminal", 1),
        )

    async def test_terminal_replay_event_is_written_before_terminal_transition(self):
        class RecordingLedger(DurableRunLedger):
            def __init__(self, path):
                super().__init__(path)
                self.operations = []

            def append_event(self, run_id, event_type, payload, **kwargs):
                self.operations.append(("event", event_type))
                return super().append_event(run_id, event_type, payload, **kwargs)

            def transition(self, run_id, status, **kwargs):
                self.operations.append(("transition", status.value))
                return super().transition(run_id, status, **kwargs)

        self.ledger.close()
        recording = RecordingLedger(os.path.join(self.tmp.name, "ordered.sqlite3"))
        self.ledger = recording
        lifecycle = DurableRunLifecycle(ledger=recording)

        async def backend():
            yield run_state_event(
                RunDisposition.COMPLETED,
                reason="ordered",
            )

        _ = [event async for event in lifecycle.stream(self._request(), backend)]

        terminal_event_index = max(
            index
            for index, operation in enumerate(recording.operations)
            if operation == ("event", "run_state")
        )
        terminal_transition_index = max(
            index
            for index, operation in enumerate(recording.operations)
            if operation == ("transition", "completed")
        )
        self.assertLess(terminal_event_index, terminal_transition_index)


if __name__ == "__main__":
    unittest.main()
