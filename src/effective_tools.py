"""Authoritative per-run tool capability calculation.

The runtime used to compose tool availability independently in the prompt,
native schema builder, relevance selector, and dispatcher.  This module keeps
the policy arithmetic pure and reviewable: callers supply the executable
catalogue for the active provider, then every consumer uses ``names``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping, Optional


WORKSPACE_INSPECTION_TOOLS = frozenset({
    "get_workspace",
    "read_file",
    "ls",
    "glob",
    "grep",
})

WORKSPACE_MUTATION_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "apply_patch",
    "todowrite",
})

# Compatibility name: a workspace makes inspection foundational.  Mutation is
# selected/authorized independently from the presence of a path.
WORKSPACE_FOUNDATIONAL_TOOLS = WORKSPACE_INSPECTION_TOOLS

SHELL_FOUNDATIONAL_TOOLS = frozenset({
    "bash",
    "python",
    "manage_bg_jobs",
})

LOOP_PRIMITIVE_TOOLS = frozenset({"ask_user", "update_plan", "manage_plan"})


@dataclass(frozen=True)
class EffectiveToolSet:
    """Immutable tool contract shared by prompt, schemas, and execution."""

    names: frozenset[str]
    foundational: frozenset[str] = frozenset()
    forced: frozenset[str] = frozenset()
    excluded_foundational: Mapping[str, str] = field(default_factory=dict)

    def allows(self, tool_name: object) -> bool:
        return isinstance(tool_name, str) and tool_name in self.names

    def diagnostic_event(self) -> dict:
        return {
            "type": "effective_tools",
            "names": sorted(self.names),
            "count": len(self.names),
            "excluded_foundational": dict(self.excluded_foundational),
            "ephemeral": True,
        }


def calculate_effective_tools(
    *,
    registered_tools: Iterable[str],
    provider_usable_tools: Iterable[str],
    relevant_tools: Optional[Iterable[str]],
    forced_tools: Optional[Iterable[str]] = None,
    disabled_tools: Optional[Iterable[str]] = None,
    security_blocked_tools: Optional[Iterable[str]] = None,
    authenticated_role: str = "unknown",
    workspace_enabled: bool = False,
    shell_enabled: bool = False,
    fallback_tools: Optional[Iterable[str]] = None,
    exclusive_tools: Optional[Iterable[str]] = None,
) -> EffectiveToolSet:
    """Return the exact executable tool set for one run.

    Relevance may reduce optional capabilities, but enabled foundational
    workspace/shell tools and explicit forced tools are added afterwards.
    Explicit disablement and the provider/executable catalogue always win.
    """

    registered = {str(name) for name in registered_tools if name}
    provider_usable = {str(name) for name in provider_usable_tools if name}
    available = registered & provider_usable
    disabled = {str(name) for name in (disabled_tools or ()) if name}
    security_blocked = {
        str(name) for name in (security_blocked_tools or ()) if name
    }
    disabled.update(security_blocked)

    selected = (
        {str(name) for name in relevant_tools if name}
        if relevant_tools is not None
        else {str(name) for name in (fallback_tools or ()) if name}
    )
    forced = {str(name) for name in (forced_tools or ()) if name}
    foundational: set[str] = set()
    if workspace_enabled:
        foundational.update(WORKSPACE_FOUNDATIONAL_TOOLS)
    if shell_enabled:
        foundational.update(SHELL_FOUNDATIONAL_TOOLS)

    if exclusive_tools is not None:
        # An explicit "use <MCP> only" boundary is stronger than relevance,
        # foundational convenience, and frontend-forced integrations.  Keep
        # only the explicitly scoped tools plus the non-effectful ask_user
        # escape hatch.
        exclusive = {str(name) for name in exclusive_tools if name}
        requested = exclusive | {"ask_user"}
        foundational.clear()
        forced.clear()
    else:
        requested = selected | forced | foundational | set(LOOP_PRIMITIVE_TOOLS)
    names = (requested & available) - disabled

    excluded: dict[str, str] = {}
    for name in sorted(foundational - names):
        if name in disabled:
            excluded[name] = (
                f"blocked by security policy for role {authenticated_role}"
                if name in security_blocked
                else "explicitly disabled for this run"
            )
        elif name not in registered:
            excluded[name] = "no registered executable handler"
        elif name not in provider_usable:
            excluded[name] = "provider invocation mode cannot expose this tool"
        else:
            excluded[name] = "unavailable"

    return EffectiveToolSet(
        names=frozenset(names),
        foundational=frozenset(foundational),
        forced=frozenset(forced & names),
        excluded_foundational=MappingProxyType(excluded),
    )
