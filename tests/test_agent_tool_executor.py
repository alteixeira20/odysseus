import asyncio

import pytest

from src.agent.execution.executor import ToolExecutionHandle


@pytest.mark.asyncio
async def test_execution_handle_streams_progress_then_returns_result():
    async def runner(progress):
        await progress({"elapsed_s": 1, "tail": "one"})
        await progress({"elapsed_s": 2, "tail": "two"})
        return "bash: ok", {"output": "ok", "exit_code": 0}

    async with ToolExecutionHandle(runner) as handle:
        updates = [update async for update in handle.progress()]
        outcome = await handle.result()

    assert updates == [
        {"elapsed_s": 1, "tail": "one"},
        {"elapsed_s": 2, "tail": "two"},
    ]
    assert outcome == ("bash: ok", {"output": "ok", "exit_code": 0})


@pytest.mark.asyncio
async def test_execution_handle_cancels_owned_task_on_stream_disconnect():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def runner(progress):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with ToolExecutionHandle(runner):
        await started.wait()

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_execution_handle_propagates_tool_failure_after_progress_closes():
    async def runner(progress):
        await progress({"tail": "before"})
        raise RuntimeError("tool failed")

    async with ToolExecutionHandle(runner) as handle:
        assert [item async for item in handle.progress()] == [
            {"tail": "before"}
        ]
        with pytest.raises(RuntimeError, match="tool failed"):
            await handle.result()
