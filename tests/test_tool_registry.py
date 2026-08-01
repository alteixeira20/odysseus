"""Coverage for the canonical tool registry (src/agent/tools/registry.py,
src/agent/tools/bootstrap.py).

Runtime V2 definitions are authoritative for the migrated coding slice;
legacy collections remain bootstrap inputs only for non-migrated tools.
These tests verify both the typed registry invariants and that compatibility
aliases cannot become a second provider-facing schema or dispatch source.
"""

from src.agent.tools.registry import (
    PROTECTED_FOUNDATIONAL_NAMES,
    ToolAutonomy,
    ToolCategory,
    ToolDefinition,
    ToolIdempotency,
    ToolRegistry,
    ToolRisk,
)


def _def(name, **overrides):
    base = dict(
        name=name,
        description=f"{name} description",
        input_schema={"type": "object", "properties": {}},
        handler=lambda content, ctx: None,
        category=ToolCategory.DATA,
        risk=ToolRisk.READ_ONLY,
        autonomy=ToolAutonomy.AUTONOMOUS,
        idempotency=ToolIdempotency.IDEMPOTENT,
    )
    base.update(overrides)
    return ToolDefinition(**base)


# ── Pure registry validation ──────────────────────────────────────────────

def test_registry_with_no_problems_validates_clean():
    # "a" isn't a protected foundational name, so a minimal single-tool
    # registry is expected to still flag every missing foundational tool —
    # that's covered separately below. This test only checks that a
    # well-formed definition itself introduces no *other* problem kind.
    reg = ToolRegistry({"a": _def("a")})
    kinds = {p.kind for p in reg.validate()}
    assert kinds == {"foundational_tool_missing"}


def test_duplicate_canonical_name_via_key_mismatch_is_caught():
    # Constructing with a mismatched dict key vs definition.name.
    reg = ToolRegistry({"wrong_key": _def("a")})
    problems = reg.validate()
    assert any(p.kind == "name_key_mismatch" for p in problems)


def test_alias_shadowing_canonical_name_is_caught():
    reg = ToolRegistry(
        {
            "a": _def("a"),
            "b": _def("b", aliases=("a",)),  # "a" is already a canonical name
        }
    )
    problems = reg.validate()
    assert any(p.kind == "alias_shadows_canonical_name" for p in problems)


def test_alias_claimed_by_two_tools_is_caught():
    reg = ToolRegistry(
        {
            "a": _def("a", aliases=("x",)),
            "b": _def("b", aliases=("x",)),
        }
    )
    problems = reg.validate()
    assert any(p.kind == "alias_collision" for p in problems)


def test_schema_without_handler_is_caught_unless_externally_dispatched():
    reg = ToolRegistry({"a": _def("a", handler=None)})
    problems = reg.validate()
    assert any(p.kind == "schema_without_handler" for p in problems)

    reg_ok = ToolRegistry({"a": _def("a", handler=None, externally_dispatched=True)})
    assert not any(p.kind == "schema_without_handler" for p in reg_ok.validate())


def test_mutating_tool_marked_read_only_is_caught():
    reg = ToolRegistry({"a": _def("a", mutates_state=True, risk=ToolRisk.READ_ONLY)})
    problems = reg.validate()
    assert any(p.kind == "mutating_tool_marked_read_only" for p in problems)


def test_destructive_tool_without_confirmation_policy_is_caught():
    reg = ToolRegistry(
        {"a": _def("a", destructive=True, autonomy=ToolAutonomy.AUTONOMOUS)}
    )
    problems = reg.validate()
    assert any(p.kind == "destructive_without_confirmation_policy" for p in problems)

    reg_ok = ToolRegistry(
        {"a": _def("a", destructive=True, autonomy=ToolAutonomy.OWNER_ONLY)}
    )
    assert not any(
        p.kind == "destructive_without_confirmation_policy" for p in reg_ok.validate()
    )


def test_long_running_without_cancellation_is_caught():
    reg = ToolRegistry(
        {"a": _def("a", long_running=True, supports_cancellation=False)}
    )
    problems = reg.validate()
    assert any(p.kind == "long_running_without_cancellation" for p in problems)


def test_frontend_events_without_lifecycle_is_caught():
    reg = ToolRegistry(
        {
            "a": _def(
                "a",
                frontend_event_types=("tool_start",),
                supports_progress=False,
                supports_background=False,
            )
        }
    )
    problems = reg.validate()
    assert any(p.kind == "frontend_events_without_lifecycle" for p in problems)


def test_missing_foundational_tool_is_caught():
    reg = ToolRegistry({"a": _def("a")})  # none of the protected names present
    problems = reg.validate()
    kinds = {p.kind for p in problems}
    assert "foundational_tool_missing" in kinds
    # Every protected name should be individually flagged.
    missing_count = sum(1 for p in problems if p.kind == "foundational_tool_missing")
    assert missing_count == len(PROTECTED_FOUNDATIONAL_NAMES)


def test_foundational_tool_present_but_handlerless_is_caught():
    defs = {name: _def(name, handler=None, externally_dispatched=True) for name in PROTECTED_FOUNDATIONAL_NAMES}
    reg = ToolRegistry(defs)
    problems = reg.validate()
    assert any(p.kind == "foundational_tool_missing_handler" for p in problems)


def test_validate_or_raise_raises_with_all_problems_listed():
    import pytest
    from src.agent.tools.registry import ToolRegistryError

    reg = ToolRegistry({"a": _def("a", mutates_state=True, risk=ToolRisk.READ_ONLY)})
    with pytest.raises(ToolRegistryError) as exc_info:
        reg.validate_or_raise()
    assert "mutating_tool_marked_read_only" in str(exc_info.value)


def test_mcp_shadowing_protected_local_tool_is_flagged():
    reg = ToolRegistry({"a": _def("a")})
    problems = reg.check_no_mcp_shadowing({"bash": object(), "some_mcp_tool": object()})
    assert len(problems) == 1
    assert problems[0].kind == "mcp_collision_with_protected_local_tool"
    assert "bash" in problems[0].detail


def test_mcp_extending_catalogue_without_shadowing_is_fine():
    reg = ToolRegistry({"a": _def("a")})
    problems = reg.check_no_mcp_shadowing({"some_new_mcp_tool": object()})
    assert problems == []


# ── Derivations ────────────────────────────────────────────────────────

def test_function_schemas_only_include_tools_with_a_schema():
    reg = ToolRegistry(
        {
            "a": _def("a", input_schema={"type": "object"}),
            "b": _def("b", input_schema={}, handler=None, externally_dispatched=True),
        }
    )
    names = {s["function"]["name"] for s in reg.function_schemas()}
    assert names == {"a"}


def test_handlers_only_include_tools_with_a_handler():
    reg = ToolRegistry(
        {
            "a": _def("a", handler=lambda c, ctx: None),
            "b": _def("b", handler=None, externally_dispatched=True),
        }
    )
    assert set(reg.handlers().keys()) == {"a"}


def test_accepted_names_includes_aliases():
    reg = ToolRegistry({"a": _def("a", aliases=("a_alias",))})
    assert reg.accepted_names() == frozenset({"a", "a_alias"})


def test_foundational_groups_derivation():
    reg = ToolRegistry(
        {
            "a": _def("a", foundational_for=frozenset({"workspace"})),
            "b": _def("b", foundational_for=frozenset({"workspace", "shell"})),
        }
    )
    groups = reg.foundational_groups()
    assert groups["workspace"] == frozenset({"a", "b"})
    assert groups["shell"] == frozenset({"b"})


def test_by_category_and_by_risk():
    reg = ToolRegistry(
        {
            "a": _def("a", category=ToolCategory.EXECUTION, risk=ToolRisk.PRIVILEGED),
            "b": _def("b", category=ToolCategory.DATA, risk=ToolRisk.READ_ONLY),
        }
    )
    assert {d.name for d in reg.by_category(ToolCategory.EXECUTION)} == {"a"}
    assert {d.name for d in reg.by_risk(ToolRisk.PRIVILEGED)} == {"a"}


def test_alias_lookup_resolves_to_canonical_definition():
    definition = _def("a", aliases=("legacy_a",))
    reg = ToolRegistry({"a": definition})
    assert reg.get("legacy_a") is definition
    assert "legacy_a" in reg
    assert reg.get("nonexistent") is None


# ── Bootstrapped from the real production tool set ────────────────────────

def test_production_registry_builds_and_validates_clean():
    from src.agent.tools.bootstrap import build_default_registry

    registry = build_default_registry()
    problems = registry.validate()
    assert problems == [], "\n".join(str(p) for p in problems)


def test_production_registry_covers_every_protected_foundational_tool():
    from src.agent.tools.bootstrap import build_default_registry

    registry = build_default_registry()
    for name in PROTECTED_FOUNDATIONAL_NAMES:
        assert name in registry, f"{name} missing from registry"
        assert registry.get(name).handler is not None


def test_production_registry_replaces_migrated_legacy_schemas():
    from src.agent_tools import TOOL_HANDLERS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.agent.tools.bootstrap import build_default_registry
    from src.agent.runtime_v2.tool_definitions import (
        MIGRATED_CANONICAL_NAMES,
        MIGRATED_LEGACY_NAMES,
    )

    registry = build_default_registry()
    legacy_schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    registry_schema_names = {s["function"]["name"] for s in registry.function_schemas()}
    migrated_legacy_schemas = (
        MIGRATED_LEGACY_NAMES | (MIGRATED_CANONICAL_NAMES & legacy_schema_names)
    )
    assert (
        registry_schema_names - MIGRATED_CANONICAL_NAMES
        == legacy_schema_names - migrated_legacy_schemas
    )
    assert MIGRATED_LEGACY_NAMES.isdisjoint(registry_schema_names)
    assert (MIGRATED_CANONICAL_NAMES - {"run_host_command"}) <= registry_schema_names

    registry_handlers = set(registry.handlers())
    legacy_handlers = set(TOOL_HANDLERS)
    assert (
        registry_handlers - MIGRATED_CANONICAL_NAMES
        == legacy_handlers - (MIGRATED_LEGACY_NAMES | MIGRATED_CANONICAL_NAMES)
    )


def test_production_registry_accepted_names_cover_tool_tags():
    from src.agent_tools import TOOL_TAGS
    from src.agent.tools.bootstrap import build_default_registry

    registry = build_default_registry()
    # Every dispatchable tag must be represented in the registry so the
    # registry is a faithful mirror of what the runtime can actually call.
    assert set(TOOL_TAGS) <= registry.accepted_names()


def test_tail_serve_output_is_reachable_via_native_tool_call():
    # Regression: tail_serve_output had a native FUNCTION_TOOL_SCHEMAS entry
    # and a real dispatch case in tool_execution.py, but was missing from
    # TOOL_TAGS — function_call_to_tool_block's `if tool_type not in
    # TOOL_TAGS: return None` gate silently rejected every native call to
    # it as "Unknown function call" before it could ever reach the
    # dispatcher. Caught by cross-checking the registry's derived
    # accepted_names() against TOOL_TAGS; fixed in src/agent_tools/__init__.py.
    import json

    from src.agent_tools import TOOL_TAGS
    from src.tool_schemas import function_call_to_tool_block

    assert "tail_serve_output" in TOOL_TAGS

    block = function_call_to_tool_block(
        "tail_serve_output", json.dumps({"session_id": "serve-abc12345", "tail": 200})
    )
    assert block is not None
    assert block.tool_type == "tail_serve_output"
