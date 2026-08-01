"""Builds a ToolRegistry from the current legacy tool sources.

This is the compatibility bootstrap described in
src/agent/tools/registry.py: it derives ToolDefinition metadata
(category/risk/autonomy/idempotency) from the existing scattered sources
(src/agent/routing/tool_domains.py's DOMAIN_TOOL_MAP, src/tool_security.py's
plan-mode/admin sets, src/tool_index.py's descriptions) rather than those
sources being generated from the registry. Production schema, name and local
handler lookups use the normalized registry after this one-time build.
Classification here is heuristic where no more specific
override is given below: derived from which existing policy sets already
mention a tool, not an independent per-tool audit. Foundational tools
(PROTECTED_FOUNDATIONAL_NAMES) have hand-written overrides since those are
the ones continuation/typed-context/planning code actually reasons about.
"""

from __future__ import annotations

from typing import Mapping

from src.agent.tools.registry import (
    ToolAutonomy,
    ToolCategory,
    ToolDefinition,
    ToolIdempotency,
    ToolRegistry,
    ToolRisk,
)

# Domain -> category for tools not covered by an explicit override below.
_DOMAIN_TO_CATEGORY = {
    "web": ToolCategory.NETWORK,
    "documents": ToolCategory.DOCUMENTS,
    "email": ToolCategory.COMMUNICATION,
    "cookbook": ToolCategory.SERVICE_CONTROL,
    "notes_calendar_tasks": ToolCategory.DATA,
    "ui": ToolCategory.USER_INTERACTION,
    "sessions": ToolCategory.DATA,
    "files": ToolCategory.FILESYSTEM,
    "settings": ToolCategory.ADMINISTRATION,
    "contacts": ToolCategory.DATA,
    "integrations": ToolCategory.INTEGRATION,
}

# Explicit per-tool overrides — the tools the runtime's continuation,
# planning, and typed-context work actually reasons about, plus anything
# the domain-map heuristic would get wrong (e.g. bash/python living in the
# "files" domain but being EXECUTION, not FILESYSTEM).
_CATEGORY_OVERRIDES: Mapping[str, ToolCategory] = {
    "bash": ToolCategory.EXECUTION,
    "python": ToolCategory.EXECUTION,
    "manage_bg_jobs": ToolCategory.EXECUTION,
    "read_file": ToolCategory.INSPECTION,
    "ls": ToolCategory.INSPECTION,
    "get_workspace": ToolCategory.INSPECTION,
    "grep": ToolCategory.SEARCH,
    "glob": ToolCategory.SEARCH,
    "search_chats": ToolCategory.SEARCH,
    "write_file": ToolCategory.EDITING,
    "edit_file": ToolCategory.EDITING,
    "apply_patch": ToolCategory.EDITING,
    "todowrite": ToolCategory.PLANNING,
    "update_plan": ToolCategory.PLANNING,
    "manage_plan": ToolCategory.PLANNING,
    "ask_user": ToolCategory.USER_INTERACTION,
    "manage_memory": ToolCategory.DATA,
    "manage_skills": ToolCategory.DATA,
    "vault_search": ToolCategory.DATA,
    "vault_get": ToolCategory.DATA,
    "vault_unlock": ToolCategory.DATA,
    "generate_image": ToolCategory.DATA,
    "edit_image": ToolCategory.DATA,
    "trigger_research": ToolCategory.DATA,
    "manage_research": ToolCategory.DATA,
    "chat_with_model": ToolCategory.INTEGRATION,
    "ask_teacher": ToolCategory.INTEGRATION,
    "list_models": ToolCategory.INTEGRATION,
    "pipeline": ToolCategory.INTEGRATION,
}

# Tools whose native schema is dispatched dynamically (MCP servers / the
# built-in email server), not through a local TOOL_HANDLERS entry — the
# registry's "schema without handler" validation must not flag these.
_EXTERNALLY_DISPATCHED = {
    "adopt_served_model", "api_call", "app_api", "archive_email", "bulk_email",
    "cancel_download", "delete_email", "download_model", "edit_image",
    "list_cached_models", "list_cookbook_servers", "list_downloads",
    "list_email_accounts", "list_emails", "list_serve_presets",
    "list_served_models", "manage_calendar", "manage_contact", "manage_memory",
    "manage_notes", "manage_skills", "manage_tasks", "mark_email_read",
    "pipeline", "read_email", "reply_to_email", "resolve_contact",
    "scan_email_unsubscribes", "search_chats", "search_hf_models",
    "send_email", "serve_model", "serve_preset", "stop_served_model",
    "tail_serve_output", "trigger_research", "unsubscribe_email",
    "ai_draft_email_reply", "download_attachment", "draft_email",
    "draft_email_reply", "generate_image", "manage_research", "search_emails",
    "ui_control",
}


def _foundational_for(name: str, workspace_set, shell_set, loop_set) -> frozenset[str]:
    groups = set()
    if name in workspace_set:
        groups.add("workspace")
    if name in shell_set:
        groups.add("shell")
    if name in loop_set:
        groups.add("loop_primitive")
    return frozenset(groups)


def build_default_registry() -> ToolRegistry:
    # Imported lazily, inside the function: src.agent_tools is the facade
    # that re-exports tool_schemas/tool_execution/etc, and importing it at
    # module scope here would make "import src.agent.tools.bootstrap" order-
    # sensitive relative to the agent_tools package's own circular-import
    # workaround (see src/tool_execution.py's `# HACK` comment).
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_index import ALWAYS_AVAILABLE, BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_security import (
        BUILTIN_EMAIL_TOOLS,
        NON_ADMIN_BLOCKED_TOOLS,
        PLAN_MODE_READONLY_TOOLS,
        _PLAN_MODE_KNOWN_MUTATORS,
    )
    from src.effective_tools import (
        LOOP_PRIMITIVE_TOOLS,
        SHELL_FOUNDATIONAL_TOOLS,
        WORKSPACE_FOUNDATIONAL_TOOLS,
        WORKSPACE_MUTATION_TOOLS,
    )
    from src.agent.routing.tool_domains import DOMAIN_TOOL_MAP

    schema_by_name = {s["function"]["name"]: s["function"] for s in FUNCTION_TOOL_SCHEMAS}
    tool_to_domain: dict[str, str] = {}
    for domain, names in DOMAIN_TOOL_MAP.items():
        for name in names:
            tool_to_domain.setdefault(name, domain)

    all_names = set(schema_by_name) | set(TOOL_HANDLERS) | set(TOOL_TAGS)

    definitions: dict[str, ToolDefinition] = {}
    for name in sorted(all_names):
        schema = schema_by_name.get(name)
        handler = TOOL_HANDLERS.get(name)
        description = (
            (schema.get("description") if schema else None)
            or BUILTIN_TOOL_DESCRIPTIONS.get(name, "")
        )
        input_schema = (schema or {}).get("parameters", {})

        category = _CATEGORY_OVERRIDES.get(
            name,
            _DOMAIN_TO_CATEGORY.get(tool_to_domain.get(name, ""), ToolCategory.DATA),
        )

        is_read_only = name in PLAN_MODE_READONLY_TOOLS
        is_known_mutator = name in _PLAN_MODE_KNOWN_MUTATORS
        is_admin_gated = name in NON_ADMIN_BLOCKED_TOOLS
        mutates = is_known_mutator or (not is_read_only and name not in {
            "ask_user", "list_models",
        })

        if name in {"bash", "python"}:
            risk = ToolRisk.PRIVILEGED
        elif name in BUILTIN_EMAIL_TOOLS or name in {"send_email", "reply_to_email", "bulk_email"}:
            risk = ToolRisk.EXTERNAL_WRITE
        elif name in {"vault_search", "vault_get", "vault_unlock"}:
            risk = ToolRisk.CREDENTIAL_ACCESS
        elif name in {
            "download_model", "serve_model", "serve_preset",
            "stop_served_model", "adopt_served_model",
        }:
            risk = ToolRisk.LONG_RUNNING
        elif is_read_only:
            risk = ToolRisk.READ_ONLY
        elif mutates:
            risk = ToolRisk.LOCAL_WRITE
        else:
            risk = ToolRisk.READ_ONLY

        destructive = name in {
            "delete_email", "manage_bg_jobs", "unsubscribe_email",
        }

        if name in {"bash", "python"}:
            # The per-turn shell toggle is the once-per-run approval boundary;
            # neither tool is autonomous merely because it was retrieved.
            autonomy = ToolAutonomy.CONFIRM_ONCE_PER_RUN
        elif is_admin_gated:
            autonomy = ToolAutonomy.OWNER_ONLY
        else:
            autonomy = ToolAutonomy.AUTONOMOUS

        if name in {"bash", "python", "read_file", "write_file", "edit_file", "grep", "glob", "ls", "apply_patch", "manage_bg_jobs", "get_workspace"}:
            idempotency = (
                ToolIdempotency.IDEMPOTENT
                if name in {"read_file", "grep", "glob", "ls", "get_workspace"}
                else ToolIdempotency.NON_IDEMPOTENT
            )
        elif is_read_only:
            idempotency = ToolIdempotency.IDEMPOTENT
        else:
            idempotency = ToolIdempotency.UNKNOWN

        definitions[name] = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
            category=category,
            risk=risk,
            autonomy=autonomy,
            idempotency=idempotency,
            foundational_for=_foundational_for(
                name, WORKSPACE_FOUNDATIONAL_TOOLS, SHELL_FOUNDATIONAL_TOOLS, LOOP_PRIMITIVE_TOOLS
            ),
            requires_workspace=name in (
                WORKSPACE_FOUNDATIONAL_TOOLS | WORKSPACE_MUTATION_TOOLS
            ),
            requires_shell_enabled=name in SHELL_FOUNDATIONAL_TOOLS,
            requires_authenticated_user=False,
            requires_role="admin" if is_admin_gated else None,
            mutates_state=mutates,
            destructive=destructive,
            long_running=name in {
                "trigger_research", "download_model", "serve_model",
                "manage_research",
            },
            supports_cancellation=True,
            supports_progress=name in {"bash", "python", "manage_bg_jobs"},
            supports_background=name in {"bash", "manage_bg_jobs", "trigger_research"},
            result_schema=None,
            frontend_event_types=(
                ("tool_start", "tool_output")
                if name in {"bash", "python", "manage_bg_jobs"}
                else ()
            ),
            externally_dispatched=name in _EXTERNALLY_DISPATCHED,
        )

    return ToolRegistry(definitions)


TOOL_REGISTRY = build_default_registry()
