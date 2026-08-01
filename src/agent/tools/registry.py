"""Canonical typed tool registry.

Root cause context: the executable tool surface is currently assembled
from independently maintained sources that must be kept in sync by hand —
``src/tool_schemas.py`` (native function schemas + the
args-dict-to-content-string adapter), ``src/agent_tools/__init__.py``
(``TOOL_HANDLERS`` + the fenced/dispatch ``TOOL_TAGS`` set),
``src/tool_index.py`` (RAG descriptions + always-available set),
``src/effective_tools.py`` (foundational/loop-primitive groupings), and
``src/tool_security.py`` (admin/plan-mode policy sets). Nothing has ever
cross-checked that these five sources agree — see the ``# HACK`` in
``src/tool_execution.py`` referencing issue #4277, which independently
flags the same drift risk for the handler map specifically.

At startup ``build_default_registry()`` normalizes those compatibility
sources into typed definitions and validates them. From that point the live
provider schema set, known-name checks, fenced parser vocabulary and local
handler lookup are derived from this registry. The legacy collections remain
bootstrap inputs for compatibility, but are no longer independent runtime
authorities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional


class ToolCategory(str, Enum):
    PLANNING = "planning"
    INSPECTION = "inspection"
    SEARCH = "search"
    FILESYSTEM = "filesystem"
    EDITING = "editing"
    EXECUTION = "execution"
    TESTING = "testing"
    VERSION_CONTROL = "version_control"
    NETWORK = "network"
    SERVICE_CONTROL = "service_control"
    DATA = "data"
    DOCUMENTS = "documents"
    COMMUNICATION = "communication"
    ADMINISTRATION = "administration"
    INTEGRATION = "integration"
    USER_INTERACTION = "user_interaction"


class ToolRisk(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"
    CREDENTIAL_ACCESS = "credential_access"
    LONG_RUNNING = "long_running"


class ToolAutonomy(str, Enum):
    AUTONOMOUS = "autonomous"
    CONFIRM_ONCE_PER_RUN = "confirm_once_per_run"
    CONFIRM_EVERY_CALL = "confirm_every_call"
    OWNER_ONLY = "owner_only"
    DISABLED_BY_DEFAULT = "disabled_by_default"


class ToolIdempotency(str, Enum):
    IDEMPOTENT = "idempotent"
    CONDITIONALLY_IDEMPOTENT = "conditionally_idempotent"
    NON_IDEMPOTENT = "non_idempotent"
    UNKNOWN = "unknown"


ToolHandler = Callable[[str, dict], Any]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Optional[ToolHandler]

    category: ToolCategory
    risk: ToolRisk
    autonomy: ToolAutonomy
    idempotency: ToolIdempotency

    aliases: tuple[str, ...] = ()
    foundational_for: frozenset[str] = frozenset()

    requires_workspace: bool = False
    requires_shell_enabled: bool = False
    requires_authenticated_user: bool = False
    requires_role: Optional[str] = None

    mutates_state: bool = False
    destructive: bool = False
    long_running: bool = False
    supports_cancellation: bool = True
    supports_progress: bool = False
    supports_background: bool = False

    result_schema: Optional[Mapping[str, Any]] = None
    frontend_event_types: tuple[str, ...] = ()

    # True for tools whose native schema comes from an external source this
    # registry doesn't own (MCP servers, email server) — validation relaxes
    # the "schema without handler" check for these, since their handler is
    # resolved dynamically at dispatch time, not through TOOL_HANDLERS.
    externally_dispatched: bool = False


class ToolRegistryError(Exception):
    """Raised by ToolRegistry.validate_or_raise() with every problem found."""


@dataclass(frozen=True)
class ToolValidationProblem:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


# Foundational local tools that MCP servers must never be allowed to shadow
# (see src/agent/tools/registry.py module docstring and the cross-cutting
# "local foundational precedence" requirement in
# specs/agent-runtime-v2-progress.md).
PROTECTED_FOUNDATIONAL_NAMES = frozenset(
    {
        "get_workspace",
        "read_file",
        "write_file",
        "edit_file",
        "apply_patch",
        "ls",
        "glob",
        "grep",
        "bash",
        "python",
        "manage_bg_jobs",
        "manage_plan",
        "ask_user",
    }
)


class ToolRegistry:
    """Immutable-after-build collection of ToolDefinitions with derivations."""

    def __init__(self, definitions: Mapping[str, ToolDefinition]):
        self._by_name: dict[str, ToolDefinition] = dict(definitions)
        self._alias_to_name: dict[str, str] = {}
        for name, definition in self._by_name.items():
            for alias in definition.aliases:
                self._alias_to_name[alias] = name

    # ── Lookup ───────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[ToolDefinition]:
        if name in self._by_name:
            return self._by_name[name]
        canonical = self._alias_to_name.get(name)
        return self._by_name.get(canonical) if canonical else None

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __iter__(self):
        return iter(self._by_name.values())

    def __len__(self) -> int:
        return len(self._by_name)

    @property
    def canonical_names(self) -> frozenset[str]:
        return frozenset(self._by_name.keys())

    # ── Derivations ──────────────────────────────────────────────────

    def function_schemas(self) -> list[dict]:
        """Native function-calling schemas, one per tool with a schema."""
        return [
            {
                "type": "function",
                "function": {
                    "name": d.name,
                    "description": d.description,
                    "parameters": d.input_schema,
                },
            }
            for d in self._by_name.values()
            if d.input_schema
        ]

    def handlers(self) -> dict[str, ToolHandler]:
        return {
            name: d.handler
            for name, d in self._by_name.items()
            if d.handler is not None
        }

    def accepted_names(self) -> frozenset[str]:
        """Canonical names + aliases — the fenced/dispatch TOOL_TAGS set."""
        names = set(self._by_name.keys())
        names.update(self._alias_to_name.keys())
        return frozenset(names)

    def foundational_groups(self) -> dict[str, frozenset[str]]:
        groups: dict[str, set[str]] = {}
        for d in self._by_name.values():
            for group in d.foundational_for:
                groups.setdefault(group, set()).add(d.name)
        return {k: frozenset(v) for k, v in groups.items()}

    def by_category(self, category: ToolCategory) -> tuple[ToolDefinition, ...]:
        return tuple(d for d in self._by_name.values() if d.category == category)

    def by_risk(self, risk: ToolRisk) -> tuple[ToolDefinition, ...]:
        return tuple(d for d in self._by_name.values() if d.risk == risk)

    # ── Validation ───────────────────────────────────────────────────

    def validate(self) -> list[ToolValidationProblem]:
        problems: list[ToolValidationProblem] = []
        seen_names: set[str] = set()

        for name, d in self._by_name.items():
            if name != d.name:
                problems.append(
                    ToolValidationProblem(
                        "name_key_mismatch",
                        f"registry key {name!r} != definition.name {d.name!r}",
                    )
                )
            if name in seen_names:
                problems.append(
                    ToolValidationProblem("duplicate_canonical_name", name)
                )
            seen_names.add(name)

            # Alias collisions: an alias must not equal another tool's
            # canonical name (shadowing), nor be claimed by two tools.
            for alias in d.aliases:
                if alias in self._by_name and alias != name:
                    problems.append(
                        ToolValidationProblem(
                            "alias_shadows_canonical_name",
                            f"{name!r} alias {alias!r} collides with "
                            "another tool's canonical name",
                        )
                    )
                owner = self._alias_to_name.get(alias)
                if owner and owner != name:
                    problems.append(
                        ToolValidationProblem(
                            "alias_collision",
                            f"alias {alias!r} claimed by both {owner!r} and {name!r}",
                        )
                    )

            if d.input_schema and d.handler is None and not d.externally_dispatched:
                problems.append(
                    ToolValidationProblem(
                        "schema_without_handler",
                        f"{name!r} has a native schema but no local handler "
                        "and is not marked externally_dispatched",
                    )
                )
            if d.handler is not None and not d.input_schema and not d.description:
                problems.append(
                    ToolValidationProblem(
                        "handler_without_definition",
                        f"{name!r} has a handler but no schema/description",
                    )
                )

            if d.mutates_state and d.risk is ToolRisk.READ_ONLY:
                problems.append(
                    ToolValidationProblem(
                        "mutating_tool_marked_read_only",
                        f"{name!r} sets mutates_state=True but risk=read_only",
                    )
                )
            if d.destructive and d.autonomy is ToolAutonomy.AUTONOMOUS:
                problems.append(
                    ToolValidationProblem(
                        "destructive_without_confirmation_policy",
                        f"{name!r} is destructive but autonomy=autonomous "
                        "(no confirmation/owner-gating policy)",
                    )
                )
            if d.long_running and not d.supports_cancellation:
                problems.append(
                    ToolValidationProblem(
                        "long_running_without_cancellation",
                        f"{name!r} is long_running but supports_cancellation=False",
                    )
                )
            if d.frontend_event_types and not d.supports_progress and not d.supports_background:
                problems.append(
                    ToolValidationProblem(
                        "frontend_events_without_lifecycle",
                        f"{name!r} declares frontend_event_types "
                        f"{d.frontend_event_types!r} but neither "
                        "supports_progress nor supports_background",
                    )
                )

        for foundational in PROTECTED_FOUNDATIONAL_NAMES:
            if foundational not in self._by_name:
                problems.append(
                    ToolValidationProblem(
                        "foundational_tool_missing",
                        f"{foundational!r} is required but not registered",
                    )
                )
            elif self._by_name[foundational].handler is None:
                problems.append(
                    ToolValidationProblem(
                        "foundational_tool_missing_handler",
                        f"{foundational!r} is registered but has no handler",
                    )
                )

        return problems

    def validate_or_raise(self) -> None:
        problems = self.validate()
        if problems:
            raise ToolRegistryError(
                f"{len(problems)} tool registry problem(s):\n"
                + "\n".join(f"  - {p}" for p in problems)
            )

    def check_no_mcp_shadowing(self, mcp_tool_names: Mapping[str, Any]) -> list[ToolValidationProblem]:
        """MCP tools may extend the catalogue but must never shadow a
        protected local foundational tool (bash, read_file, ask_user, ...).
        """
        problems = []
        for name in mcp_tool_names:
            if name in PROTECTED_FOUNDATIONAL_NAMES:
                problems.append(
                    ToolValidationProblem(
                        "mcp_collision_with_protected_local_tool",
                        f"MCP tool {name!r} collides with a protected "
                        "foundational local tool",
                    )
                )
        return problems
