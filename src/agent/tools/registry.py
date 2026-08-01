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
import hashlib
import json
from typing import Any, Callable, Mapping, Optional

import jsonschema

from src.execution_policy import ExecutionMode
from src.agent.runtime_v2.contracts import (
    AgentExecutionContext,
    Capability,
    Effect,
    ToolResult,
)
from src.agent.runtime_v2.effect_policy import DefinitionApprovalPolicy


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


ToolHandler = Callable[..., Any]
EffectResolver = Callable[
    [Mapping[str, Any], AgentExecutionContext], tuple[Effect, ...]
]
ExposurePolicy = Callable[[Optional[AgentExecutionContext]], bool]
ArgumentAdapter = Callable[[Any, str], Mapping[str, Any]]


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

    # Runtime V2 fields. Legacy definitions retain compatibility defaults;
    # migrated definitions populate every field explicitly and are the only
    # source for schema, validation, policy, dispatch, and frontend metadata.
    required_capabilities: frozenset[Capability] = frozenset()
    effect_resolver: Optional[EffectResolver] = None
    approval_policy: DefinitionApprovalPolicy = (
        DefinitionApprovalPolicy.CAPABILITY_ONLY
    )
    exposure_policy: Optional[ExposurePolicy] = None
    timeout_seconds: float = 30.0
    provider_schema: Optional[Mapping[str, Any]] = None
    argument_adapter: Optional[ArgumentAdapter] = None
    runtime_v2: bool = False

    def validate_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise ValueError(f"{self.name} arguments must be an object")
        value = dict(arguments)
        if self.input_schema:
            try:
                jsonschema.Draft202012Validator(self.input_schema).validate(value)
            except jsonschema.ValidationError as exc:
                path = ".".join(str(item) for item in exc.absolute_path)
                location = f" at {path}" if path else ""
                raise ValueError(
                    f"invalid arguments for {self.name}{location}: {exc.message}"
                ) from exc
        return value

    def resolve_effects(
        self,
        arguments: Mapping[str, Any],
        context: AgentExecutionContext,
    ) -> tuple[Effect, ...]:
        if self.effect_resolver is None:
            return ()
        return tuple(self.effect_resolver(arguments, context))

    def validate_result(self, result: ToolResult) -> ToolResult:
        if not isinstance(result, ToolResult):
            raise ValueError(f"{self.name} handler returned a non-ToolResult")
        if result.canonical_name != self.name:
            raise ValueError(
                f"{self.name} handler returned result for {result.canonical_name!r}"
            )
        if not result.call_id:
            raise ValueError(f"{self.name} result is missing call_id")
        if self.result_schema:
            try:
                jsonschema.Draft202012Validator(self.result_schema).validate(
                    result.as_dict()
                )
            except jsonschema.ValidationError as exc:
                raise ValueError(
                    f"malformed result for {self.name}: {exc.message}"
                ) from exc
        return result

    def exposed(self, context: Optional[AgentExecutionContext]) -> bool:
        return self.exposure_policy(context) if self.exposure_policy else True


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
        "workspace_context",
        "read_files",
        "patch_workspace",
        "find_files",
        "search_text",
        "run_sandbox_command",
        "run_host_command",
        "run_python",
        "ask_user",
        "plan",
    }
)

PROTECTED_FOUNDATIONAL_ALIASES = frozenset(
    {
        "bash",
        "python",
        "get_workspace",
        "glob",
        "ls",
        "grep",
        "rg",
        "read_file",
        "write_file",
        "edit_file",
        "apply_patch",
        "update_plan",
        "manage_plan",
        "todowrite",
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

    def resolve(
        self,
        name: str,
        context: Optional[AgentExecutionContext] = None,
    ) -> Optional[ToolDefinition]:
        raw = str(name or "")
        if raw == "bash" and context is not None:
            target = (
                "run_host_command"
                if context.execution_mode is ExecutionMode.HOST
                else "run_sandbox_command"
            )
            return self._by_name.get(target)
        return self.get(raw)

    def canonical_name(
        self,
        name: str,
        context: Optional[AgentExecutionContext] = None,
    ) -> Optional[str]:
        definition = self.resolve(name, context)
        return definition.name if definition is not None else None

    def canonicalize_names(
        self,
        names,
        context: Optional[AgentExecutionContext] = None,
    ) -> frozenset[str]:
        canonical: set[str] = set()
        for name in names or ():
            raw = str(name)
            if raw == "bash" and context is None:
                canonical.update({"run_sandbox_command", "run_host_command"})
                continue
            resolved = self.canonical_name(raw, context)
            canonical.add(resolved or raw)
        return frozenset(canonical)

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

    def function_schemas(
        self,
        context: Optional[AgentExecutionContext] = None,
    ) -> list[dict]:
        """Native function-calling schemas, one per tool with a schema."""
        return [
            dict(d.provider_schema)
            if d.provider_schema is not None
            else {
                "type": "function",
                "function": {
                    "name": d.name,
                    "description": d.description,
                    "parameters": d.input_schema,
                },
            }
            for d in self._by_name.values()
            if d.input_schema and d.exposed(context)
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

    @property
    def revision(self) -> str:
        payload = [
            {
                "name": definition.name,
                "aliases": sorted(definition.aliases),
                "schema": definition.input_schema,
                "runtime_v2": definition.runtime_v2,
            }
            for definition in self._by_name.values()
        ]
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

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
            if d.runtime_v2:
                if d.handler is None:
                    problems.append(
                        ToolValidationProblem(
                            "runtime_v2_handler_missing",
                            f"{name!r} has no authoritative handler",
                        )
                    )
                if d.effect_resolver is None:
                    problems.append(
                        ToolValidationProblem(
                            "runtime_v2_effect_resolver_missing",
                            f"{name!r} has no argument-aware effect resolver",
                        )
                    )
                if d.result_schema is None:
                    problems.append(
                        ToolValidationProblem(
                            "runtime_v2_result_schema_missing",
                            f"{name!r} has no canonical result schema",
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
            lifecycle_events = set(d.frontend_event_types) - {
                "tool_started",
                "tool_result",
            }
            if lifecycle_events and not d.supports_progress and not d.supports_background:
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
        protected_names = set(PROTECTED_FOUNDATIONAL_NAMES)
        protected_names.update(PROTECTED_FOUNDATIONAL_ALIASES)
        for canonical in PROTECTED_FOUNDATIONAL_NAMES:
            definition = self._by_name.get(canonical)
            if definition is not None:
                protected_names.update(definition.aliases)
        problems = []
        for name in mcp_tool_names:
            if name in protected_names:
                problems.append(
                    ToolValidationProblem(
                        "mcp_collision_with_protected_local_tool",
                        f"MCP tool {name!r} collides with a protected "
                        "foundational local tool",
                    )
                )
        return problems
