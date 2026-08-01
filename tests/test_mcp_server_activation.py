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
async def test_explicit_named_server_exposes_read_only_tools_but_withholds_mutators(monkeypatch):
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

    read_only = {
        _qualified(SERENA_ID, "read_file"),
        _qualified(SERENA_ID, "find_symbol"),
        _qualified(SERENA_ID, "find_referencing_symbols"),
        _qualified(SERENA_ID, "search_for_pattern"),
        _qualified(SERENA_ID, "read_memory"),
    }
    assert read_only <= _schema_names(captured)
    assert _qualified(SERENA_ID, "replace_symbol_body") not in _schema_names(captured)
    assert _qualified(SERENA_ID, "check_onboarding_performed") not in _schema_names(captured)
    activation = next(e for e in captured["events"] if e.get("type") == "mcp_activation")
    assert activation["exclusive"] is False
    assert activation["diagnostics"][0]["code"] == "partial_effect_scope"


@pytest.mark.asyncio
async def test_exclusive_named_server_keeps_only_readonly_server_tools_and_ask_user(monkeypatch):
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
    assert _qualified(SERENA_ID, "find_symbol") in names
    assert _qualified(SERENA_ID, "replace_symbol_body") not in names
    assert "ask_user" in names
    assert "update_plan" not in names
    assert not {"bash", "python", "write_file", "edit_file", "apply_patch"} & names
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
    assert activation["diagnostics"][0]["code"] == "partial_effect_scope"


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
    assert _qualified(SERENA_ID, "bash") not in decision.activated_tool_names
    assert _qualified(SERENA_ID, "bash") in decision.withheld_tool_names

    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    async def local_bash(*args, **kwargs):
        return {"output": str(tmp_path.resolve()), "exit_code": 0}
    monkeypatch.setattr(tool_execution, "_direct_fallback", local_bash)
    description, result = await tool_execution.execute_tool_block(
        ToolBlock("bash", "pwd"),
        owner="admin",
        workspace=str(tmp_path),
        allowed_tools={"bash", _qualified(SERENA_ID, "bash")},
        execution_mode="sandboxed",
    )
    assert description.startswith("bash:")
    assert result["exit_code"] == 0
    assert str(tmp_path.resolve()) in result["output"]


@pytest.mark.parametrize(
    "message",
    [
        "Do not use Serena.",
        "Never use Serena.",
        "Don't use Serena for this.",
        "Use anything except Serena.",
        "Avoid Serena.",
        "Work without Serena.",
    ],
)
def test_negated_server_directives_activate_no_tools(message):
    manager = _manager(include_other=False)
    decision = resolve_mcp_activation(message, manager.get_server_catalog())

    assert decision.explicit is True
    assert decision.requested_server_ids == frozenset()
    assert decision.excluded_server_ids == {SERENA_ID}
    assert decision.activated_tool_names == frozenset()


def test_latest_scope_correction_wins():
    manager = _manager(include_other=False)
    decision = resolve_mcp_activation(
        "Do not use Serena. Actually, use Serena to inspect the repository.",
        manager.get_server_catalog(),
    )

    assert decision.requested_server_ids == {SERENA_ID}
    assert decision.excluded_server_ids == frozenset()
    assert _qualified(SERENA_ID, "find_symbol") in decision.activated_tool_names


def test_explicit_mutation_intent_can_authorize_non_destructive_server_mutator():
    manager = _manager(include_other=False)
    decision = resolve_mcp_activation(
        "Use Serena to replace the symbol body and implement the requested fix.",
        manager.get_server_catalog(),
    )

    assert _qualified(SERENA_ID, "replace_symbol_body") in decision.activated_tool_names


def test_negated_mutation_intent_withholds_server_mutators():
    manager = _manager(include_other=False)
    decision = resolve_mcp_activation(
        "Use Serena to inspect. Do not edit or delete anything.",
        manager.get_server_catalog(),
    )

    assert _qualified(SERENA_ID, "find_symbol") in decision.activated_tool_names
    assert _qualified(SERENA_ID, "replace_symbol_body") not in decision.activated_tool_names
