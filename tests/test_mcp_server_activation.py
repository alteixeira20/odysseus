"""Production-backed coverage for explicit, server-scoped MCP activation."""

import json

import pytest

from src import agent_loop
from src.agent.tools.mcp_activation import resolve_mcp_activation
from src.agent_tools import ToolBlock
from src.mcp_manager import McpManager


SERENA_ID = "server-7d9f"
OTHER_ID = "repo-intel-2a1c"
SERENA_TOOL_NAMES = (
    "check_onboarding_performed",
    "read_file",
    "find_symbol",
    "find_referencing_symbols",
    "search_for_pattern",
    "read_memory",
    "replace_symbol_body",
)


def _tool(name):
    return {
        "name": name,
        "description": f"Serena capability {name}",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    }


def _manager(*, serena_status="connected", include_other=True):
    manager = McpManager()
    manager._connections[SERENA_ID] = {
        "status": serena_status,
        "name": "Serena",
    }
    if serena_status == "connected":
        manager._tools[SERENA_ID] = [_tool(name) for name in SERENA_TOOL_NAMES]
    if include_other:
        manager._connections[OTHER_ID] = {
            "status": "connected",
            "name": "Repository Intelligence",
        }
        manager._tools[OTHER_ID] = [_tool("inspect_repository")]
    return manager


def _qualified(server_id, name):
    return f"mcp__{server_id}__{name}"


def _decode_events(chunks):
    return [
        json.loads(chunk[6:])
        for chunk in chunks
        if chunk.startswith("data: {")
    ]


async def _capture_runtime(
    monkeypatch,
    *,
    manager,
    message,
    relevant_tools,
    mcp_disabled_map=None,
    disabled_tools=None,
):
    monkeypatch.setattr(agent_loop, "get_mcp_manager", lambda: manager)
    monkeypatch.setattr(agent_loop, "blocked_tools_for_owner", lambda owner: set())
    monkeypatch.setattr(agent_loop, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(
        agent_loop,
        "_load_mcp_disabled_map",
        lambda: dict(mcp_disabled_map or {}),
    )
    try:
        from services.memory.skills import SkillsManager

        monkeypatch.setattr(SkillsManager, "load", lambda self, owner=None: [])
    except Exception:
        pass

    captured = {}

    def prompt(messages, model, active_document, mcp_mgr, disabled, **kwargs):
        allowed = set(kwargs.get("effective_tool_names") or ())
        schemas = [
            schema
            for schema in manager.get_all_openai_schemas(
                kwargs.get("mcp_disabled_map") or {}
            )
            if schema["function"]["name"] in allowed
        ]
        captured["activation_notice"] = kwargs.get("mcp_activation_notice", "")
        return ([{"role": "system", "content": captured["activation_notice"]}] + list(messages), schemas)

    monkeypatch.setattr(agent_loop, "_build_system_prompt", prompt)

    async def provider(candidates, messages, **kwargs):
        captured["schemas"] = kwargs.get("tools") or []
        captured["messages"] = messages
        yield 'data: {"delta": "done"}\n\n'
        yield 'data: {"type": "finish", "reason": "stop"}\n\n'
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(agent_loop, "stream_llm_with_fallback", provider)
    chunks = [
        chunk
        async for chunk in agent_loop.stream_agent_loop(
            "https://api.openai.com/v1",
            "gpt-4o",
            [{"role": "user", "content": message}],
            relevant_tools=set(relevant_tools),
            disabled_tools=set(disabled_tools or ()),
            owner="admin",
            shell_enabled=False,
            max_rounds=1,
            _is_teacher_run=True,
        )
    ]
    captured["events"] = _decode_events(chunks)
    return captured


def _schema_names(captured):
    return {schema["function"]["name"] for schema in captured["schemas"]}


def test_ordinary_turn_keeps_relevance_based_mcp_selection():
    manager = _manager()
    selected = {_qualified(SERENA_ID, "find_symbol")}
    decision = resolve_mcp_activation(
        "Inspect the symbol graph",
        manager.get_server_catalog(),
    )

    assert decision.explicit is False
    assert decision.apply(selected) == selected


@pytest.mark.asyncio
async def test_ordinary_runtime_turn_exposes_only_relevant_mcp_tool(monkeypatch):
    manager = _manager()
    selected = _qualified(SERENA_ID, "find_symbol")
    captured = await _capture_runtime(
        monkeypatch,
        manager=manager,
        message="Inspect the symbol graph.",
        relevant_tools={selected},
    )

    mcp_names = {name for name in _schema_names(captured) if name.startswith("mcp__")}
    assert mcp_names == {selected}
    assert not [e for e in captured["events"] if e.get("type") == "mcp_activation"]


@pytest.mark.asyncio
async def test_explicit_named_server_exposes_every_enabled_tool_to_provider(monkeypatch):
    manager = _manager()
    captured = await _capture_runtime(
        monkeypatch,
        manager=manager,
        message="Use Serena to inspect this repository.",
        # Simulate top-k retrieval returning only the two observed incident tools.
        relevant_tools={
            _qualified(SERENA_ID, "check_onboarding_performed"),
            _qualified(SERENA_ID, "replace_symbol_body"),
        },
    )

    expected = {_qualified(SERENA_ID, name) for name in SERENA_TOOL_NAMES}
    assert expected <= _schema_names(captured)
    assert {
        _qualified(SERENA_ID, "read_file"),
        _qualified(SERENA_ID, "find_symbol"),
        _qualified(SERENA_ID, "find_referencing_symbols"),
        _qualified(SERENA_ID, "search_for_pattern"),
        _qualified(SERENA_ID, "read_memory"),
    } <= _schema_names(captured)
    activation = next(e for e in captured["events"] if e.get("type") == "mcp_activation")
    assert activation["exclusive"] is False
    assert activation["diagnostics"][0]["code"] == "activated"


@pytest.mark.asyncio
async def test_exclusive_named_server_excludes_other_mcp_but_keeps_controls(monkeypatch):
    manager = _manager()
    other = _qualified(OTHER_ID, "inspect_repository")
    captured = await _capture_runtime(
        monkeypatch,
        manager=manager,
        message="Use Serena only to inspect the repository.",
        relevant_tools={other, "ask_user", "update_plan"},
    )

    names = _schema_names(captured)
    assert other not in names
    assert {_qualified(SERENA_ID, name) for name in SERENA_TOOL_NAMES} <= names
    assert {"ask_user", "update_plan"} <= names
    assert "Exclusive MCP scope is active" in captured["activation_notice"]


@pytest.mark.asyncio
async def test_disabled_server_and_run_tools_remain_absent(monkeypatch):
    manager = _manager(include_other=False)
    server_disabled = "replace_symbol_body"
    run_disabled = _qualified(SERENA_ID, "read_memory")
    captured = await _capture_runtime(
        monkeypatch,
        manager=manager,
        message="Use Serena.",
        relevant_tools={"ask_user"},
        mcp_disabled_map={SERENA_ID: {server_disabled}},
        disabled_tools={run_disabled},
    )

    names = _schema_names(captured)
    assert _qualified(SERENA_ID, server_disabled) not in names
    assert run_disabled not in names
    activation = next(e for e in captured["events"] if e.get("type") == "mcp_activation")
    assert activation["diagnostics"][0]["code"] == "partial_disabled"


@pytest.mark.asyncio
async def test_disconnected_named_server_produces_model_and_sse_diagnostic(monkeypatch):
    manager = _manager(serena_status="error", include_other=False)
    captured = await _capture_runtime(
        monkeypatch,
        manager=manager,
        message="Use Serena to inspect the project.",
        relevant_tools={"ask_user"},
    )

    activation = next(e for e in captured["events"] if e.get("type") == "mcp_activation")
    assert activation["diagnostics"][0]["code"] == "server_disconnected"
    assert "is error" in captured["activation_notice"]
    assert not {name for name in _schema_names(captured) if name.startswith("mcp__")}


@pytest.mark.asyncio
async def test_same_named_mcp_tool_cannot_shadow_local_bash(tmp_path, monkeypatch):
    from src import tool_execution

    manager = _manager(include_other=False)
    manager._tools[SERENA_ID].append(_tool("bash"))
    catalog = manager.get_server_catalog()
    decision = resolve_mcp_activation("Use Serena", catalog)
    assert _qualified(SERENA_ID, "bash") in decision.activated_tool_names

    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    description, result = await tool_execution.execute_tool_block(
        ToolBlock("bash", "pwd"),
        owner="admin",
        workspace=str(tmp_path),
        allowed_tools={"bash", _qualified(SERENA_ID, "bash")},
    )
    assert description.startswith("bash:")
    assert result["exit_code"] == 0
    assert str(tmp_path.resolve()) in result["output"]
