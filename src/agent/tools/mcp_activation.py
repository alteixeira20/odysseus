"""Resolve explicit, server-scoped MCP activation for one agent turn.

Ordinary tool selection is intentionally relevance based.  A user who names a
connected server (``Use Serena``), however, is selecting a capability boundary,
not asking the embedding index to guess which three tools they meant.  This
module resolves that boundary from the manager's stable server metadata and
returns a pure decision that the orchestration layer can compose with relevance,
disabled-tool, authorization, and provider policy.

There are deliberately no server-specific names or qualified-name prefixes in
this module.  Server IDs and display names come from ``McpManager``; qualified
tool names come from its catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping, Optional, Sequence


def _clean_label(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _alias_pattern(alias: str) -> str:
    # Server labels are metadata, not regex.  Flexible whitespace is useful for
    # labels such as "Serena MCP" without weakening the token boundaries.
    escaped = re.escape(_clean_label(alias)).replace(r"\ ", r"\s+")
    return rf"(?<![\w-]){escaped}(?![\w-])"


def _request_match(text: str, alias: str) -> Optional[re.Match[str]]:
    if not alias:
        return None
    label = _alias_pattern(alias)
    patterns = (
        rf"\bonly\s+use\s+(?:the\s+)?{label}(?:\s+(?:mcp\s+)?server)?",
        rf"\buse\s+only\s+(?:the\s+)?{label}(?:\s+(?:mcp\s+)?server)?",
        rf"\buse\s+(?:the\s+)?{label}(?:\s+(?:mcp\s+)?server)?(?:\s+only\b)?",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match
    return None


def _match_is_exclusive(text: str, match: re.Match[str]) -> bool:
    before = text[max(0, match.start() - 16) : match.start()]
    matched = match.group(0)
    after = text[match.end() : match.end() + 12]
    return bool(
        re.search(r"\bonly\s*$", before, flags=re.IGNORECASE)
        or re.search(r"\bonly\s+use\b", matched, flags=re.IGNORECASE)
        or re.search(r"\buse\s+only\b", matched, flags=re.IGNORECASE)
        or re.search(r"\bonly\b", after, flags=re.IGNORECASE)
        or re.search(r"\bonly\s*$", matched, flags=re.IGNORECASE)
    )


@dataclass(frozen=True)
class McpActivationDiagnostic:
    server_id: str
    server_name: str
    status: str
    code: str
    enabled_tool_count: int
    disabled_tool_count: int
    message: str

    def as_dict(self) -> dict:
        return {
            "server_id": self.server_id,
            "server_name": self.server_name,
            "status": self.status,
            "code": self.code,
            "enabled_tool_count": self.enabled_tool_count,
            "disabled_tool_count": self.disabled_tool_count,
            "message": self.message,
        }


@dataclass(frozen=True)
class McpActivationDecision:
    explicit: bool = False
    exclusive: bool = False
    requested_server_ids: frozenset[str] = frozenset()
    activated_tool_names: frozenset[str] = frozenset()
    excluded_tool_names: frozenset[str] = frozenset()
    diagnostics: tuple[McpActivationDiagnostic, ...] = ()

    def apply(self, selected_tools: Optional[Iterable[str]]) -> Optional[set[str]]:
        """Compose explicit server scope with relevance-selected tool names."""

        if not self.explicit:
            return None if selected_tools is None else set(selected_tools)
        selected = set(selected_tools or ())
        if self.exclusive:
            selected.difference_update(self.excluded_tool_names)
        selected.update(self.activated_tool_names)
        return selected

    def event(self) -> dict:
        return {
            "type": "mcp_activation",
            "explicit": self.explicit,
            "exclusive": self.exclusive,
            "requested_server_ids": sorted(self.requested_server_ids),
            "activated_tool_names": sorted(self.activated_tool_names),
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "ephemeral": True,
        }

    def prompt_notice(self) -> str:
        if not self.explicit:
            return ""
        lines = [
            "The runtime resolved the user's explicit MCP server request for this turn.",
        ]
        for diagnostic in self.diagnostics:
            lines.append(f"- {diagnostic.message}")
        if self.exclusive:
            lines.append(
                "Exclusive MCP scope is active: tools from other MCP servers are "
                "unavailable. Essential local agent-control tools may still be available."
            )
        lines.append(
            "Disabled tools, authorization rules, and security policy still take precedence."
        )
        return "\n".join(lines)


def resolve_mcp_activation(
    user_text: str,
    server_catalog: Sequence[Mapping[str, object]],
) -> McpActivationDecision:
    """Resolve ``Use <server> [only]`` against a stable MCP server catalogue.

    ``server_catalog`` is the metadata snapshot returned by
    :meth:`McpManager.get_server_catalog`.  Disconnected servers remain in the
    snapshot so an explicit request can produce a useful diagnostic instead of
    silently degrading to a random subset (or no tools).
    """

    text = str(user_text or "")
    if not re.search(r"\buse\b", text, flags=re.IGNORECASE):
        return McpActivationDecision()

    name_counts: dict[str, int] = {}
    for server in server_catalog:
        normalized = _clean_label(server.get("server_name")).casefold()
        if normalized:
            name_counts[normalized] = name_counts.get(normalized, 0) + 1

    selected: list[Mapping[str, object]] = []
    matches: list[re.Match[str]] = []
    ambiguous_names: set[str] = set()
    for server in server_catalog:
        server_id = _clean_label(server.get("server_id"))
        server_name = _clean_label(server.get("server_name") or server_id)
        id_match = _request_match(text, server_id)
        name_match = _request_match(text, server_name)
        if id_match:
            selected.append(server)
            matches.append(id_match)
            continue
        if name_match:
            if name_counts.get(server_name.casefold(), 0) > 1:
                ambiguous_names.add(server_name)
                matches.append(name_match)
                continue
            selected.append(server)
            matches.append(name_match)

    if not selected and not ambiguous_names:
        return McpActivationDecision()

    exclusive = any(_match_is_exclusive(text, match) for match in matches)
    requested_ids: set[str] = set()
    activated: set[str] = set()
    diagnostics: list[McpActivationDiagnostic] = []

    for name in sorted(ambiguous_names):
        diagnostics.append(
            McpActivationDiagnostic(
                server_id="",
                server_name=name,
                status="ambiguous",
                code="ambiguous_server_name",
                enabled_tool_count=0,
                disabled_tool_count=0,
                message=(
                    f"MCP server name {name!r} is ambiguous; use its stable server ID."
                ),
            )
        )

    for server in selected:
        server_id = _clean_label(server.get("server_id"))
        server_name = _clean_label(server.get("server_name") or server_id)
        status = _clean_label(server.get("status") or "disconnected").lower()
        tools = tuple(server.get("tools") or ())
        enabled_tools = [tool for tool in tools if not tool.get("is_disabled")]
        disabled_count = len(tools) - len(enabled_tools)
        requested_ids.add(server_id)

        if status != "connected":
            code = "server_disconnected"
            message = (
                f"Requested MCP server {server_name!r} is {status or 'disconnected'}; "
                "none of its tools are available."
            )
        elif not enabled_tools:
            code = "no_enabled_tools"
            message = (
                f"Requested MCP server {server_name!r} is connected but has no enabled tools."
            )
        else:
            activated.update(
                str(tool.get("qualified_name"))
                for tool in enabled_tools
                if tool.get("qualified_name")
            )
            code = "partial_disabled" if disabled_count else "activated"
            suffix = (
                f"; {disabled_count} disabled tool(s) remain unavailable"
                if disabled_count
                else ""
            )
            message = (
                f"Activated all {len(enabled_tools)} enabled tool(s) from MCP server "
                f"{server_name!r}{suffix}."
            )

        diagnostics.append(
            McpActivationDiagnostic(
                server_id=server_id,
                server_name=server_name,
                status=status,
                code=code,
                enabled_tool_count=len(enabled_tools),
                disabled_tool_count=disabled_count,
                message=message,
            )
        )

    excluded: set[str] = set()
    if exclusive:
        for server in server_catalog:
            if _clean_label(server.get("server_id")) in requested_ids:
                continue
            if _clean_label(server.get("status")).lower() != "connected":
                continue
            for tool in tuple(server.get("tools") or ()):
                if not tool.get("is_disabled") and tool.get("qualified_name"):
                    excluded.add(str(tool["qualified_name"]))

    return McpActivationDecision(
        explicit=True,
        exclusive=exclusive,
        requested_server_ids=frozenset(requested_ids),
        activated_tool_names=frozenset(activated),
        excluded_tool_names=frozenset(excluded),
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "McpActivationDecision",
    "McpActivationDiagnostic",
    "resolve_mcp_activation",
]
