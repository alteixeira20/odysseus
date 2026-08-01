"""Cancellation-safe provider stream pumping for one agent round."""

import asyncio
import json
from typing import AsyncGenerator

from src.agent.events import run_status_event


async def stream_with_idle_status(
    provider_stream,
    *,
    interval_s: float = 2.0,
) -> AsyncGenerator[str, None]:
    """Pump provider events and emit bounded ephemeral idle status."""

    queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        try:
            async for item in provider_stream:
                await queue.put(("chunk", item))
        except asyncio.CancelledError as exc:
            # A provider may use cancellation to report a disconnected
            # upstream request. Preserve that terminal signal for the owning
            # run instead of turning it into an ordinary end-of-stream.
            queue.put_nowait(("error", exc))
            raise
        except BaseException as exc:
            await queue.put(("error", exc))
        finally:
            await queue.put(("done", None))

    task = asyncio.create_task(pump())
    phase = "waiting_for_first_byte"
    label = "Waiting for provider"
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(
                    queue.get(),
                    timeout=interval_s,
                )
            except asyncio.TimeoutError:
                yield run_status_event(phase, label)
                continue
            if kind == "done":
                break
            if kind == "error":
                raise value
            chunk = value
            if isinstance(chunk, str) and chunk.startswith("data: "):
                try:
                    event = json.loads(chunk[6:])
                except Exception:
                    event = {}
                if event.get("type") == "run_status":
                    phase = str(event.get("phase") or phase)
                    label = {
                        "waiting_local_capacity": "Waiting for local capacity",
                        "contacting_provider": "Contacting provider",
                    }.get(phase, event.get("label") or label)
            yield chunk
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        close = getattr(provider_stream, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:
                pass
