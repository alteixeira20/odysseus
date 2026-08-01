"""Parse and enforce explicit, server-scoped MCP directives for one turn."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Mapping, Optional, Sequence


_READONLY_PREFIXES = (
    "list", "get", "read", "search", "fetch", "query", "find", "describe",
    "show", "view", "lookup", "count", "status", "info", "inspect", "summar",
)


def _mcp_tool_is_readonly(tool: Mapping[str, object]) -> bool:
    annotations = tool.get("annotations")
    if isinstance(annotations, Mapping):
        read_hint = annotations.get("readOnlyHint")
        destructive = annotations.get("destructiveHint")
    else:
        read_hint = getattr(annotations, "readOnlyHint", None)
        destructive = getattr(annotations, "destructiveHint", None)
    if read_hint is True:
        return True
    if read_hint is False or destructive is True:
        return False
    return str(tool.get("name") or "").casefold().startswith(_READONLY_PREFIXES)


def _clean_label(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _alias_pattern(alias: str) -> str:
    escaped = re.escape(_clean_label(alias)).replace(r"\ ", r"\s+")
    return rf"(?<![\w-]){escaped}(?![\w-])"


_DIRECTIVE_MARKERS = (
    ("do not use", "exclude"),
    ("don't use", "exclude"),
    ("dont use", "exclude"),
    ("never use", "exclude"),
    ("avoid", "exclude"),
    ("without", "exclude"),
    ("except", "exclude"),
    ("use", "request"),
)


@dataclass(frozen=True)
class ToolScopeDirective:
    requested: frozenset[str] = frozenset()
    excluded: frozenset[str] = frozenset()
    exclusive: bool = False
    ambiguous_names: frozenset[str] = frozenset()


def _directive_for_match(text: str, match: re.Match[str]) -> tuple[str, bool]:
    """Return (request|exclude|none, exclusive) for one server mention.

    This is a small directive parser: it selects the nearest directive marker
    before the alias instead of searching for a positive phrase anywhere in the
    sentence.  Consequently a nearer ``do not use``/``except`` cannot be
    overridden by the substring ``use`` inside it.
    """

    clause_start = max(
        text.rfind(".", 0, match.start()),
        text.rfind(";", 0, match.start()),
        text.rfind("\n", 0, match.start()),
    ) + 1
    prefix = text[clause_start : match.start()].casefold()
    nearest: tuple[int, str] = (-1, "none")
    exclusion_spans: list[tuple[int, int]] = []
    for marker, kind in _DIRECTIVE_MARKERS:
        for marker_match in re.finditer(rf"\b{re.escape(marker)}\b", prefix):
            if kind == "exclude":
                exclusion_spans.append(marker_match.span())
            position = marker_match.start()
            if position > nearest[0]:
                nearest = (position, kind)
    # The positive word "use" inside "do not use" / "never use" is not a
    # second directive. Prefer the containing negative phrase.
    for start, end in exclusion_spans:
        if start <= nearest[0] < end:
            nearest = (start, "exclude")
            break
    kind = nearest[1]
    if kind == "none":
        return kind, False

    local_before = prefix[max(0, nearest[0] - 12) :]
    local_after = text[match.end() : match.end() + 24].casefold()
    exclusive = kind == "request" and bool(
        re.search(r"\bonly\s+(?:use\s+)?$", local_before)
        or re.search(r"\buse\s+only\s*$", local_before)
        or re.match(r"\s+(?:mcp\s+server\s+)?only\b", local_after)
    )
    return kind, exclusive


def parse_tool_scope_directive(
    user_text: str,
    server_catalog: Sequence[Mapping[str, object]],
) -> ToolScopeDirective:
    text = str(user_text or "")
    name_counts: dict[str, int] = {}
    for server in server_catalog:
        name = _clean_label(server.get("server_name")).casefold()
        if name:
            name_counts[name] = name_counts.get(name, 0) + 1

    requested: set[str] = set()
    excluded: set[str] = set()
    ambiguous: set[str] = set()
    exclusive = False
    for server in server_catalog:
        server_id = _clean_label(server.get("server_id"))
        server_name = _clean_label(server.get("server_name") or server_id)
        matches: list[tuple[re.Match[str], bool]] = []
        matches.extend(
            (match, False)
            for match in re.finditer(_alias_pattern(server_id), text, re.IGNORECASE)
        )
        matches.extend(
            (match, True)
            for match in re.finditer(_alias_pattern(server_name), text, re.IGNORECASE)
        )
        if not matches:
            continue
        # The user's latest explicit correction wins ("don't use Serena —
        # actually, use Serena"). Stable-ID and display-name matches at the
        # same location prefer the stable ID so duplicate names stay safe.
        match, used_name = max(
            matches,
            key=lambda item: (item[0].start(), not item[1]),
        )
        kind, mention_exclusive = _directive_for_match(text, match)
        if kind == "none":
            continue
        if used_name and name_counts.get(server_name.casefold(), 0) > 1:
            ambiguous.add(server_name)
            continue
        if kind == "exclude":
            excluded.add(server_id)
            requested.discard(server_id)
        else:
            requested.add(server_id)
            excluded.discard(server_id)
            exclusive = exclusive or mention_exclusive

    return ToolScopeDirective(
        requested=frozenset(requested),
        excluded=frozenset(excluded),
        exclusive=exclusive,
        ambiguous_names=frozenset(ambiguous),
    )


def _has_mutation_intent(text: str) -> bool:
    mutation = re.compile(
        r"\b(?:apply|chang(?:e|es|ed|ing)|creat(?:e|es|ed|ing)|"
        r"delet(?:e|es|ed|ing)|edit(?:s|ed|ing)?|fix(?:es|ed|ing)?|"
        r"implement(?:s|ed|ing)?|modif(?:y|ies|ied|ying)|mov(?:e|es|ed|ing)|"
        r"patch(?:es|ed|ing)?|refactor(?:s|ed|ing)?|renam(?:e|es|ed|ing)|"
        r"replac(?:e|es|ed|ing)|updat(?:e|es|ed|ing)|writ(?:e|es|ten|ing))\b"
    )
    negation = re.compile(
        r"\b(?:no|not|never|avoid|without|don't|dont|do\s+not)\b"
    )
    # Evaluate coherent clauses so one explicit denial covers coordinated
    # verbs ("do not edit or delete"), while a later correction after
    # punctuation/contrast can still grant a mutation intentionally.
    clauses = re.split(
        r"(?:[.;\n]+|\bbut\b|\bhowever\b|\binstead\b)",
        str(text or "").casefold(),
    )
    for clause in clauses:
        if mutation.search(clause) and not negation.search(clause):
            return True
    return False


def _tool_is_destructive(tool: Mapping[str, object]) -> bool:
    annotations = tool.get("annotations")
    if isinstance(annotations, Mapping):
        return annotations.get("destructiveHint") is True
    return bool(getattr(annotations, "destructiveHint", False))


def _tool_is_credential_access(tool: Mapping[str, object]) -> bool:
    name = str(tool.get("name") or "").casefold()
    return any(
        word in name
        for word in ("credential", "secret", "token", "password", "private_key")
    )


def _tool_specifically_requested(tool: Mapping[str, object], text: str) -> bool:
    name = str(tool.get("name") or "").strip()
    if not name:
        return False
    alias = _alias_pattern(name)
    for match in re.finditer(alias, str(text or ""), re.IGNORECASE):
        clause_start = max(
            text.rfind(".", 0, match.start()),
            text.rfind(";", 0, match.start()),
            text.rfind("\n", 0, match.start()),
        ) + 1
        prefix = text[clause_start : match.start()].casefold()
        if re.search(r"(?:do\s+not|don't|dont|never|avoid|without)\s+(?:\w+\s+){0,2}$", prefix):
            continue
        if re.search(r"\b(?:use|call|run|invoke|read|get|fetch)\s+(?:the\s+)?$", prefix):
            return True
    return False


def mcp_tool_allowed_for_turn(
    tool: Mapping[str, object], user_text: str
) -> bool:
    """Fail-closed effect policy composed with server/relevance selection."""

    if _tool_is_destructive(tool):
        return False
    if _tool_is_credential_access(tool):
        return _tool_specifically_requested(tool, str(user_text or ""))
    return _mcp_tool_is_readonly(tool) or _has_mutation_intent(user_text)


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
    excluded_server_ids: frozenset[str] = frozenset()
    activated_tool_names: frozenset[str] = frozenset()
    withheld_tool_names: frozenset[str] = frozenset()
    excluded_tool_names: frozenset[str] = frozenset()
    diagnostics: tuple[McpActivationDiagnostic, ...] = ()

    def apply(self, selected_tools: Optional[Iterable[str]]) -> Optional[set[str]]:
        if not self.explicit:
            return None if selected_tools is None else set(selected_tools)
        selected = set(selected_tools or ())
        selected.difference_update(self.excluded_tool_names)
        selected.difference_update(self.withheld_tool_names)
        if self.exclusive:
            return set(self.activated_tool_names) | {"ask_user"}
        selected.update(self.activated_tool_names)
        return selected

    def event(self) -> dict:
        return {
            "type": "mcp_activation",
            "explicit": self.explicit,
            "exclusive": self.exclusive,
            "requested_server_ids": sorted(self.requested_server_ids),
            "excluded_server_ids": sorted(self.excluded_server_ids),
            "activated_tool_names": sorted(self.activated_tool_names),
            "withheld_tool_names": sorted(self.withheld_tool_names),
            "diagnostics": [item.as_dict() for item in self.diagnostics],
            "ephemeral": True,
        }

    def prompt_notice(self) -> str:
        if not self.explicit:
            return ""
        lines = ["The runtime resolved the user's MCP scope directive for this turn."]
        lines.extend(f"- {item.message}" for item in self.diagnostics)
        if self.exclusive:
            lines.append(
                "Exclusive MCP scope is active: only authorized tools from the "
                "requested server plus ask_user are available."
            )
        lines.append(
            "Disabled tools, effect authorization, and security policy take precedence."
        )
        return "\n".join(lines)


def resolve_mcp_activation(
    user_text: str,
    server_catalog: Sequence[Mapping[str, object]],
) -> McpActivationDecision:
    directive = parse_tool_scope_directive(user_text, server_catalog)
    if not (
        directive.requested
        or directive.excluded
        or directive.ambiguous_names
    ):
        return McpActivationDecision()

    requested_tools: set[str] = set()
    activated: set[str] = set()
    withheld: set[str] = set()
    excluded_tools: set[str] = set()
    diagnostics: list[McpActivationDiagnostic] = []

    for name in sorted(directive.ambiguous_names):
        diagnostics.append(
            McpActivationDiagnostic(
                server_id="",
                server_name=name,
                status="ambiguous",
                code="ambiguous_server_name",
                enabled_tool_count=0,
                disabled_tool_count=0,
                message=f"MCP server name {name!r} is ambiguous; use its stable server ID.",
            )
        )

    for server in server_catalog:
        server_id = _clean_label(server.get("server_id"))
        server_name = _clean_label(server.get("server_name") or server_id)
        status = _clean_label(server.get("status") or "disconnected").lower()
        tools = tuple(server.get("tools") or ())
        enabled = [tool for tool in tools if not tool.get("is_disabled")]
        disabled_count = len(tools) - len(enabled)
        qualified = {
            str(tool.get("qualified_name"))
            for tool in enabled
            if tool.get("qualified_name")
        }

        if server_id in directive.excluded:
            excluded_tools.update(qualified)
            diagnostics.append(
                McpActivationDiagnostic(
                    server_id=server_id,
                    server_name=server_name,
                    status=status,
                    code="explicitly_excluded",
                    enabled_tool_count=len(enabled),
                    disabled_tool_count=disabled_count,
                    message=f"MCP server {server_name!r} is explicitly excluded for this turn.",
                )
            )
            continue
        if server_id not in directive.requested:
            if directive.exclusive:
                excluded_tools.update(qualified)
            continue

        requested_tools.update(qualified)
        if status != "connected":
            code = "server_disconnected"
            message = (
                f"Requested MCP server {server_name!r} is {status}; none of its tools are available."
            )
        elif not enabled:
            code = "no_enabled_tools"
            message = f"Requested MCP server {server_name!r} has no enabled tools."
        else:
            for tool in enabled:
                name = str(tool.get("qualified_name") or "")
                if not name:
                    continue
                if mcp_tool_allowed_for_turn(tool, user_text):
                    activated.add(name)
                else:
                    withheld.add(name)
            code = "partial_effect_scope" if withheld & qualified else (
                "partial_disabled" if disabled_count else "activated"
            )
            message = (
                f"Authorized {len(activated & qualified)} of {len(enabled)} enabled tool(s) "
                f"from MCP server {server_name!r}; mutating, destructive, or credential "
                "effects remain withheld unless explicitly authorized."
            )
        diagnostics.append(
            McpActivationDiagnostic(
                server_id=server_id,
                server_name=server_name,
                status=status,
                code=code,
                enabled_tool_count=len(enabled),
                disabled_tool_count=disabled_count,
                message=message,
            )
        )

    # A disconnected server may still have stale catalogue entries.  Never
    # activate them even if their names survived in the snapshot.
    activated.intersection_update(requested_tools)
    return McpActivationDecision(
        explicit=True,
        exclusive=directive.exclusive,
        requested_server_ids=directive.requested,
        excluded_server_ids=directive.excluded,
        activated_tool_names=frozenset(activated),
        withheld_tool_names=frozenset(withheld),
        excluded_tool_names=frozenset(excluded_tools),
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "McpActivationDecision",
    "McpActivationDiagnostic",
    "ToolScopeDirective",
    "mcp_tool_allowed_for_turn",
    "parse_tool_scope_directive",
    "resolve_mcp_activation",
]
