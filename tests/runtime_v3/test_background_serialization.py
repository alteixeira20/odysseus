import asyncio
import unittest

from src import agent_runs


class BackgroundSerializationTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_start_if_idle_never_replaces_running_foreground_work(self):
        gate = asyncio.Event()

        async def foreground():
            await gate.wait()
            yield "data: [DONE]\n\n"

        async def background():
            yield "data: [DONE]\n\n"

        current = agent_runs.start(
            "session-busy",
            foreground(),
            mode=agent_runs.RunMode.DETACHED,
            owner="owner",
        )
        candidate = background()
        blocked = agent_runs.start_if_idle(
            "session-busy",
            candidate,
            mode=agent_runs.RunMode.BACKGROUND,
            owner="owner",
        )
        self.assertIsNone(blocked)
        self.assertIs(agent_runs._RUNS["session-busy"], current)
        await candidate.aclose()
        gate.set()
        await current.task

    async def test_terminal_callback_runs_before_session_becomes_idle(self):
        observations = []

        async def source():
            yield "data: [DONE]\n\n"

        def terminal_callback(terminal):
            observations.append(
                {
                    "active": agent_runs.is_active("session-terminal"),
                    "reason": terminal.reason,
                }
            )

        run = agent_runs.start_if_idle(
            "session-terminal",
            source(),
            mode=agent_runs.RunMode.BACKGROUND,
            owner="owner",
            terminal_callback=terminal_callback,
        )
        self.assertIsNotNone(run)
        async for _event in agent_runs.subscribe("session-terminal"):
            pass
        self.assertEqual(len(observations), 1)
        self.assertTrue(observations[0]["active"])
        self.assertFalse(agent_runs.is_active("session-terminal"))


if __name__ == "__main__":
    unittest.main()
