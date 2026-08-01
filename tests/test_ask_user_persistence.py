"""Regression coverage for durable ``ask_user`` choice cards.

The live event must arrive after ``tool_output`` so the settled tool trace
cannot cover/push away the card.  The same payload must be persisted inside
``tool_events`` so chat history can reconstruct it after a reload.
"""

import asyncio
import json
from pathlib import Path

import src.agent_loop as agent_loop
from src.agent.runtime_v2.contracts import ToolResult, ToolResultStatus


ROOT = Path(__file__).resolve().parents[1]


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def _events(chunks):
    events = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            events.append(json.loads(chunk[6:]))
    return events


def test_ask_user_is_emitted_last_and_persisted(monkeypatch):
    payload = {
        "question": "¿Qué proyecto prefieres?",
        "options": [
            {"label": "Análisis de reseñas"},
            {"label": "Clasificación temática"},
        ],
        "multi": False,
    }

    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(agent_loop, "estimate_tokens", lambda *args, **kwargs: 10, raising=False)

    async def fake_stream(_candidates, messages, **kwargs):
        call = {"name": "ask_user", "arguments": json.dumps(payload, ensure_ascii=False)}
        yield f'data: {json.dumps({"type": "tool_calls", "calls": [call]})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_execute(call, execution_context, **kwargs):
        assert call.arguments["question"] == payload["question"]
        return ToolResult(
            call_id=call.call_id,
            canonical_name=call.canonical_name,
            status=ToolResultStatus.SUCCESS,
            data={
                "ask_user": payload,
                "text": "Awaiting their selection.",
            },
            backend="test",
        )

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", fake_stream, raising=False)
    monkeypatch.setattr(
        "src.agent.execution.batch_runner.execute_normalized_tool_call",
        fake_execute,
    )

    chunks = _collect(
        agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": "Ayúdame a elegir un proyecto."}],
            relevant_tools={"ask_user"},
            _is_teacher_run=True,
        )
    )
    events = _events(chunks)

    tool_output_index = next(
        i
        for i, event in enumerate(events)
        if event.get("type") == "tool_result" and event.get("version") == 2
    )
    ask_user_index = next(i for i, event in enumerate(events) if event.get("type") == "ask_user")
    assert tool_output_index < ask_user_index

    tool_output = events[tool_output_index]
    assert tool_output["payload"]["result"]["data"]["ask_user"] == payload
    assert "¿Qué proyecto prefieres?" in tool_output["payload"]["command"]
    assert "\\u00" not in tool_output["payload"]["command"]

    metrics = next(event["data"] for event in events if event.get("type") == "metrics")
    assert metrics["tool_events"][0]["ask_user"] == payload


def test_frontend_uses_one_renderer_for_live_and_restored_cards():
    chat = (ROOT / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    renderer = (ROOT / "static" / "js" / "chatRenderer.js").read_text(encoding="utf-8")

    assert "chatRenderer.renderAskUserCard(json.data || {})" in chat
    assert "export function renderAskUserCard" in renderer
    assert "renderAskUserCard(pendingAskUser" in renderer
    assert "if (role === 'user') removeAskUserCards(box)" in renderer
