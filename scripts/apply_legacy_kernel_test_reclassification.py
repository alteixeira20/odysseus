#!/usr/bin/env python3
"""Retarget only legacy-kernel characterization fixtures after loop inversion."""

from pathlib import Path


def replace_once_in_function(path: Path, function_marker: str, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    start = text.index(function_marker)
    next_function = text.find("\n\n@pytest", start + len(function_marker))
    next_plain = text.find("\n\ndef ", start + len(function_marker))
    next_async = text.find("\n\nasync def ", start + len(function_marker))
    candidates = [pos for pos in (next_function, next_plain, next_async) if pos != -1]
    end = min(candidates) if candidates else len(text)
    block = text[start:end]
    assert block.count(old) == 1, (path, function_marker, block.count(old))
    block = block.replace(old, new, 1)
    path.write_text(text[:start] + block + text[end:], encoding="utf-8")


replay = Path("tests/test_agent_replay_golden.py")
replace_once_in_function(
    replay,
    "async def test_replay_matches_golden_event_sequence(",
    "async for chunk in agent_loop.stream_agent_loop(\n",
    "async for chunk in agent_loop._legacy_stream_agent_kernel(\n",
)

runtime = Path("tests/test_agent_runtime_contract.py")
replace_once_in_function(
    runtime,
    "async def test_concurrent_agent_runs_keep_provider_tool_scopes_isolated(",
    "async for event in agent_loop.stream_agent_loop(\n",
    "async for event in agent_loop._legacy_stream_agent_kernel(\n",
)
replace_once_in_function(
    runtime,
    "async def _capture_agent_contract(\n",
    "async for event in agent_loop.stream_agent_loop(\n",
    "async for event in agent_loop._legacy_stream_agent_kernel(\n",
)
replace_once_in_function(
    runtime,
    "async def test_failed_fenced_tool_can_be_corrected_next_round(",
    "async for event in agent_loop.stream_agent_loop(\n",
    "async for event in agent_loop._legacy_stream_agent_kernel(\n",
)

for path in (replay, runtime):
    source = path.read_text(encoding="utf-8")
    assert "agent_loop._legacy_stream_agent_kernel(" in source
