import json

import pytest

from core import database
from src import agent_loop
from src.agent.rounds.tool_calls import resolve_round_tool_calls
from src.tool_execution import execute_tool_block


def test_unknown_native_call_becomes_owned_recoverable_block():
    resolved = resolve_round_tool_calls(
        "",
        [
            {
                "id": "call-one",
                "name": "web_serch",
                "arguments": '{"query":"weather"}',
            }
        ],
        1,
        is_api_model=True,
    )

    assert resolved.used_native
    assert len(resolved.tool_blocks) == 1
    assert resolved.tool_blocks[0].tool_type == "web_serch"
    assert resolved.unknown_calls[0].name == "web_serch"
    assert "web_search" in resolved.unknown_calls[0].suggestions


@pytest.mark.asyncio
async def test_unknown_marker_returns_structured_retryable_result():
    resolved = resolve_round_tool_calls(
        "",
        [
            {
                "id": "call-one",
                "name": "web_serch",
                "arguments": "{}",
            }
        ],
        1,
        is_api_model=True,
    )

    description, result = await execute_tool_block(
        resolved.tool_blocks[0],
        allowed_tools={"web_search"},
    )

    assert description == "unknown: web_serch"
    assert result["error_type"] == "unknown_tool"
    assert result["retryable"] is True
    assert result["suggestions"][0] == "web_search"


@pytest.mark.asyncio
async def test_agent_continues_after_unknown_native_tool(monkeypatch):
    class EmptyQuery:
        def filter(self, *args, **kwargs):
            return self

        def first(self):
            return None

    class EmptyDatabase:
        def query(self, *args, **kwargs):
            return EmptyQuery()

        def close(self):
            pass

    monkeypatch.setattr(database, "SessionLocal", lambda: EmptyDatabase())
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
        lambda messages, *args, **kwargs: (
            [{"role": "system", "content": "SYSTEM"}] + list(messages),
            [],
        ),
    )

    provider_round = 0

    async def provider(*args, **kwargs):
        nonlocal provider_round
        provider_round += 1
        if provider_round == 1:
            payload = {
                "type": "tool_calls",
                "calls": [
                    {
                        "id": "bad-one",
                        "name": "web_serch",
                        "arguments": '{"query":"weather"}',
                    }
                ],
            }
        else:
            payload = {"delta": "Recovered with the available tool contract."}
        yield f"data: {json.dumps(payload)}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)

    chunks = [
        chunk
        async for chunk in agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-test",
            [{"role": "user", "content": "Look up weather"}],
            relevant_tools={"web_search"},
            max_rounds=3,
            owner="admin",
        )
    ]
    events = [
        json.loads(chunk[6:])
        for chunk in chunks
        if chunk.startswith("data: ")
        and chunk != "data: [DONE]\n\n"
    ]

    unknown_output = next(
        event
        for event in events
        if event.get("type") == "tool_output"
    )
    assert unknown_output["tool"] == "web_serch"
    assert unknown_output["error_type"] == "unknown_tool"
    assert any(
        event.get("delta")
        == "Recovered with the available tool contract."
        for event in events
    )
