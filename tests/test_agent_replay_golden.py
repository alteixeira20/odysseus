"""Deterministic provider replays and golden legacy event sequences."""

import hashlib
import json
from pathlib import Path

import pytest

from core import database
from src import agent_loop


FIXTURES = (
    Path(__file__).resolve().parent / "fixtures" / "agent_replays"
)


class _EmptyQuery:
    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return None


class _EmptyDatabase:
    def query(self, *args, **kwargs):
        return _EmptyQuery()

    def close(self):
        pass


def _canonical_events(chunks):
    events = []
    for chunk in chunks:
        if chunk == "data: [DONE]\n\n":
            events.append({"done": True})
            continue
        assert chunk.startswith("data: "), chunk
        event = json.loads(chunk[6:])
        event_type = event.get("type")
        if event_type == "run_status":
            events.append(
                {
                    key: event[key]
                    for key in ("type", "phase", "label", "tool", "round")
                    if key in event
                }
            )
            continue
        if event_type == "metrics":
            events.append(
                {
                    "type": "metrics",
                    "model": (event.get("data") or {}).get("model"),
                }
            )
            continue
        if event_type == "effective_tools":
            events.append(
                {
                    "type": "effective_tools",
                    "names": event.get("names"),
                }
            )
            continue
        for volatile in (
            "duration",
            "elapsed_s",
            "round_elapsed_s",
            "timings",
            "invocation_id",
            "timeout_seconds",
        ):
            event.pop(volatile, None)
        events.append(event)
    return events


def _patch_runtime(monkeypatch, replay):
    monkeypatch.setattr(database, "SessionLocal", lambda: _EmptyDatabase())
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(
        agent_loop,
        "blocked_tools_for_owner",
        lambda owner: set(),
    )
    monkeypatch.setattr(
        agent_loop,
        "get_setting",
        lambda key, default=None: default,
    )
    monkeypatch.setattr(
        agent_loop,
        "estimate_tokens",
        lambda *args, **kwargs: 10,
    )
    monkeypatch.setattr(
        agent_loop,
        "_build_system_prompt",
        lambda messages, model, active_document, mcp_mgr, disabled_tools, **kwargs: (
            [{"role": "system", "content": "REPLAY SYSTEM"}] + list(messages),
            [],
        ),
    )
    monkeypatch.setattr(
        agent_loop.secrets,
        "token_urlsafe",
        lambda size=16: "replay-nonce",
    )

    provider_round = 0

    async def provider(*args, **kwargs):
        nonlocal provider_round
        round_events = replay["provider_rounds"][provider_round]
        provider_round += 1
        for event in round_events:
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(
        agent_loop,
        "stream_llm_with_fallback",
        provider,
    )

    if replay.get("tool_result"):
        async def execute(*args, **kwargs):
            tool_result = replay["tool_result"]
            return tool_result["description"], dict(tool_result["result"])

        monkeypatch.setattr(agent_loop, "execute_tool_block", execute)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture_name",
    ("plain_no_tool.json", "native_tool_call.json"),
)
async def test_replay_matches_golden_event_sequence(
    fixture_name,
    monkeypatch,
):
    replay = json.loads(
        (FIXTURES / fixture_name).read_text(encoding="utf-8")
    )
    _patch_runtime(monkeypatch, replay)

    chunks = [
        chunk
        async for chunk in agent_loop.stream_agent_loop(
            replay["endpoint_url"],
            replay["model"],
            replay["messages"],
            relevant_tools=set(replay["relevant_tools"]),
            shell_enabled=replay["shell_enabled"],
            owner=replay.get("owner"),
            max_rounds=len(replay["provider_rounds"]) + 1,
        )
    ]

    assert _canonical_events(chunks) == replay["expected_events"]


@pytest.mark.parametrize(
    ("tools", "compact", "length", "digest"),
    (
        (
            {"ask_user", "update_plan"},
            False,
            2030,
            "2769a2810838b46939434f2a8ee9691a652241021a3f9ea04d2c30a6fe1d0876",
        ),
        (
            # Length/digest updated for the new "## Shell rules" fragment
            # (src/agent/prompting/contexts/shell_guidance.py), appended
            # only when `bash` is in the tool set — see
            # domain_rules_for_tools in src/agent/routing/tool_domains.py.
            {"ask_user", "update_plan", "bash", "manage_bg_jobs"},
            True,
            4215,
            "af00f499e88b871d91f5de2b19857816920c7c63f9aba7a7385d02c8558dc6d9",
        ),
        (
            {
                "ask_user",
                "create_document",
                "update_document",
                "edit_document",
            },
            False,
            3266,
            "7881cb2f1b540d099582d620c49270011883103dd67f2479a28c08446e5d7e94",
        ),
    ),
)
def test_prompt_pack_snapshot(
    monkeypatch,
    tools,
    compact,
    length,
    digest,
):
    monkeypatch.setattr(
        agent_loop,
        "get_builtin_overrides",
        lambda: {},
    )
    prompt = agent_loop._assemble_prompt(tools, compact=compact)

    assert len(prompt) == length
    assert hashlib.sha256(prompt.encode()).hexdigest() == digest
