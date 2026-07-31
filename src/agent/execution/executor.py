"""Cancellation-owned execution handle for one foreground agent tool."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Optional


ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]
ToolRunner = Callable[
    [ProgressCallback],
    Awaitable[tuple[str, dict[str, Any]]],
]


class ToolExecutionHandle:
    """Own a tool task, its progress queue, and disconnect cancellation."""

    def __init__(self, runner: ToolRunner):
        self._runner = runner
        self._progress: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "ToolExecutionHandle":
        if self._task is not None:
            raise RuntimeError("tool execution handle cannot be reused")
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        return False

    async def _push_progress(self, payload: dict[str, Any]) -> None:
        await self._progress.put(payload)

    async def _run(self) -> tuple[str, dict[str, Any]]:
        try:
            return await self._runner(self._push_progress)
        finally:
            await self._progress.put(None)

    async def progress(self) -> AsyncIterator[dict[str, Any]]:
        if self._task is None:
            raise RuntimeError("enter the tool execution handle first")
        while True:
            payload = await self._progress.get()
            if payload is None:
                break
            yield payload

    async def result(self) -> tuple[str, dict[str, Any]]:
        if self._task is None:
            raise RuntimeError("enter the tool execution handle first")
        return await self._task
