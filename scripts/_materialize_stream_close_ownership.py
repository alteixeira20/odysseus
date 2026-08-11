from __future__ import annotations

from pathlib import Path
import py_compile
import subprocess


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one transform anchor, found {count}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/agent/api.py",
    '''async def stream(request: AgentRunRequest) -> AsyncGenerator[str, None]:\n    async for event in DEFAULT_AGENT_RUNNER.stream(request):\n        yield event\n''',
    '''async def stream(request: AgentRunRequest) -> AsyncGenerator[str, None]:\n    delegated = DEFAULT_AGENT_RUNNER.stream(request)\n    try:\n        async for event in delegated:\n            yield event\n    finally:\n        await delegated.aclose()\n''',
)

replace_once(
    "src/agent/api.py",
    '''    async for event in stream(request):\n        yield event\n''',
    '''    delegated = stream(request)\n    try:\n        async for event in delegated:\n            yield event\n    finally:\n        await delegated.aclose()\n''',
)

replace_once(
    "src/agent/runner.py",
    '''        async for event in durable:\n            yield event\n''',
    '''        try:\n            async for event in durable:\n                yield event\n        finally:\n            aclose = getattr(durable, "aclose", None)\n            if callable(aclose):\n                await aclose()\n''',
)

replace_once(
    "src/agent_loop.py",
    '''    async for event in canonical_stream_agent_loop(\n''',
    '''    delegated = canonical_stream_agent_loop(\n''',
)
replace_once(
    "src/agent_loop.py",
    '''        execution_context=execution_context,\n    ):\n        yield event\n''',
    '''        execution_context=execution_context,\n    )\n    try:\n        async for event in delegated:\n            yield event\n    finally:\n        await delegated.aclose()\n''',
)

for path in ("src/agent/api.py", "src/agent/runner.py", "src/agent_loop.py"):
    py_compile.compile(path, doraise=True)

subprocess.run(["git", "diff", "--check"], check=True)
print("stream-close ownership materialized successfully")
