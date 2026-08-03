import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from src import agent_runs
from src.agent.runtime_v3.contracts import RunStatus
from src.agent.runtime_v3.ledger import DurableRunLedger


class ExactCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for run in list(agent_runs._RUNS.values()):
            if run.task and not run.task.done():
                run.task.cancel()
        await asyncio.sleep(0)
        agent_runs._RUNS.clear()

    async def asyncTearDown(self):
        for run in list(agent_runs._RUNS.values()):
            if run.task and not run.task.done():
                run.task.cancel()
        await asyncio.sleep(0)
        agent_runs._RUNS.clear()

    async def test_old_durable_run_cannot_cancel_newer_session_run(self):
        gate = asyncio.Event()

        async def sleeper():
            await gate.wait()

        cancellation = SimpleNamespace(cancelled=False)

        def cancel():
            cancellation.cancelled = True

        cancellation.cancel = cancel
        context = SimpleNamespace(run_id="new-run", cancellation_token=cancellation)
        run = agent_runs._Run(agent_runs.RunMode.DETACHED, "owner", context)
        run.task = asyncio.create_task(sleeper())
        agent_runs._RUNS["session"] = run

        self.assertFalse(agent_runs.stop("session", expected_run_id="old-run"))
        self.assertFalse(run.task.done())
        self.assertFalse(cancellation.cancelled)

        self.assertTrue(agent_runs.stop("session", expected_run_id="new-run"))
        await asyncio.sleep(0)
        self.assertTrue(run.task.cancelled())
        self.assertTrue(cancellation.cancelled)


class ReplayRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = DurableRunLedger(Path(self.tmp.name) / "ledger.sqlite3")
        self.ledger.create_run(
            run_id="run-retention",
            session_id="session",
            owner="owner",
            workload="test",
            request={"message_count": 1},
            limits={},
            model="model",
            endpoint="http://127.0.0.1:11434/v1",
        )
        self.ledger.transition("run-retention", RunStatus.RUNNING)

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def test_event_count_is_bounded_and_gap_is_explicit(self):
        with patch.dict(
            os.environ,
            {
                "ODYSSEUS_AGENT_MAX_REPLAY_EVENTS": "128",
                "ODYSSEUS_AGENT_MAX_REPLAY_BYTES": str(64 * 1024 * 1024),
            },
            clear=False,
        ):
            for number in range(140):
                self.ledger.append_event(
                    "run-retention",
                    "delta",
                    {"number": number, "text": "x" * 32},
                )
        window = self.ledger.event_window("run-retention")
        replay = self.ledger.replay("run-retention", after_seq=0, limit=1000)
        self.assertEqual(window["last_event_seq"], 140)
        self.assertEqual(window["available_from_seq"], 13)
        self.assertEqual(len(replay), 128)
        self.assertEqual(replay[0]["seq"], 13)
        self.assertGreater(window["retained_bytes"], 0)


class DeploymentIntegrationTests(unittest.TestCase):
    def test_no_temporary_mcp_runtime_install_path_remains(self):
        source = Path("src/builtin_mcp.py").read_text(encoding="utf-8")
        self.assertIn("mcp-server-playwright", source)
        self.assertIn("_find_local_node_binary", source)
        self.assertNotIn("_find_npx", source)
        self.assertNotIn("npx -y", source)
        self.assertNotIn("_npx_cache", source)

    def test_runtime_routes_report_replay_gaps(self):
        source = Path("routes/agent_runtime_routes.py").read_text(encoding="utf-8")
        self.assertIn('event: replay_gap', source)
        self.assertIn('"available_from_seq"', source)
        self.assertIn('"truncated"', source)

    def test_docker_and_setup_use_lockfile_install(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        setup = Path("setup.py").read_text(encoding="utf-8")
        self.assertIn("npm ci --omit=dev --ignore-scripts", dockerfile)
        self.assertIn("node_modules/.bin/mcp-server-playwright", dockerfile)
        self.assertIn("install_node_runtime", setup)
        self.assertIn("mcp-server-playwright", setup)


if __name__ == "__main__":
    unittest.main()
