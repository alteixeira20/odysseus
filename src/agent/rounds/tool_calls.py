"""Normalize native and textual provider tool calls for one round."""

from dataclasses import dataclass
from difflib import get_close_matches
import json
import logging
import re
import secrets
from typing import Any

from src.agent.tools.bootstrap import TOOL_REGISTRY
from src.agent.runtime_v2.contracts import AgentExecutionContext, NormalizedToolCall

TOOL_TAGS = TOOL_REGISTRY.accepted_names()

logger = logging.getLogger(__name__)

_FENCE_MARKER_RE = re.compile(r"```")
_FENCE_OPEN_TAG_RE = re.compile(
    r"```(" + "|".join(re.escape(t) for t in sorted(TOOL_TAGS)) + r")(?![\w-])",
    re.IGNORECASE,
)


def _has_unclosed_tool_fence(text: str) -> bool:
    """True when a recognized tool-call fence was opened but never closed.

    A stream cut off mid tool call leaves the opening ```<tag> in the text
    with no matching closing ```. An odd number of ``` markers means the
    last one has no partner; only treat that as a truncated TOOL call (not
    an ordinary unbalanced code fence in prose) when it is tagged with a
    name from the recognized tool vocabulary.
    """
    if not text:
        return False
    positions = [m.start() for m in _FENCE_MARKER_RE.finditer(text)]
    if len(positions) % 2 == 0:
        return False
    tail = text[positions[-1]:]
    return bool(_FENCE_OPEN_TAG_RE.match(tail))


@dataclass(frozen=True)
class UnknownToolCall:
    name: str
    arguments: Any
    call_id: str | None = None
    suggestions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResolvedToolCalls:
    tool_blocks: list
    used_native: bool
    converted_calls: list
    unknown_calls: tuple[UnknownToolCall, ...] = ()
    # Native call names whose argument JSON failed to parse — the strongest
    # available signal that the provider's stream was cut off mid tool-call
    # (see src/agent/providers/finish_reason.py). Distinct from unknown_calls,
    # which covers calls to tool names the runtime doesn't recognize.
    incomplete_native_calls: tuple[str, ...] = ()
    # True when the round's raw text opens a recognized fenced tool call
    # (```bash, ```read_file, ...) that never closes. parse_tool_blocks()
    # only returns complete blocks, so this never executes — it exists so
    # the truncation classifier can tell "cut off mid fenced call" apart
    # from "deliberately finished with no tool call".
    fenced_call_unclosed: bool = False


@dataclass(frozen=True)
class QwenToolFilterResult:
    tool_blocks: list
    converted_calls: list
    native_tool_calls: list
    dropped_memory_lookup: bool = False

    @property
    def requires_memory_answer_retry(self) -> bool:
        return self.dropped_memory_lookup and not self.tool_blocks


def normalize_tool_calls(
    tool_blocks: list,
    converted_calls: list,
    *,
    execution_context: AgentExecutionContext,
    provider_name: str,
) -> tuple[NormalizedToolCall, ...]:
    """Collapse parsed/native/alias forms into the executor's only call type."""

    normalized: list[NormalizedToolCall] = []
    for index, block in enumerate(tool_blocks):
        native = (
            converted_calls[index]
            if index < len(converted_calls) and isinstance(converted_calls[index], dict)
            else {}
        )
        raw_name = str(native.get("name") or block.tool_type or "")
        definition = TOOL_REGISTRY.resolve(raw_name, execution_context)
        canonical_name = definition.name if definition is not None else raw_name
        call_id = str(native.get("id") or secrets.token_urlsafe(18))
        raw_arguments: Any = (
            block.arguments
            if isinstance(getattr(block, "arguments", None), dict)
            else block.content
        )
        normalization_error = None
        if definition is not None and definition.runtime_v2:
            try:
                adapter = definition.argument_adapter
                arguments = (
                    dict(adapter(raw_arguments, raw_name))
                    if adapter is not None
                    else dict(raw_arguments or {})
                )
                arguments = definition.validate_arguments(arguments)
            except (TypeError, ValueError) as exc:
                arguments = {}
                normalization_error = str(exc)
        elif isinstance(raw_arguments, dict):
            arguments = dict(raw_arguments)
        else:
            raw_text = str(raw_arguments or "").strip()
            try:
                parsed = json.loads(raw_text) if raw_text.startswith("{") else None
            except (TypeError, ValueError):
                parsed = None
            arguments = parsed if isinstance(parsed, dict) else {}
        normalized.append(
            NormalizedToolCall(
                call_id=call_id,
                canonical_name=canonical_name,
                arguments=arguments,
                provider_name=str(provider_name or "unknown"),
                raw_name=raw_name,
                legacy_content=str(block.content or ""),
                normalization_error=normalization_error,
            )
        )
    return tuple(normalized)


def filter_odysseus_qwen_calls(
    tool_blocks: list,
    converted_calls: list,
    native_tool_calls: list,
    *,
    used_native: bool,
    latest_user_text: str,
) -> QwenToolFilterResult:
    """Apply the finetune's explicit-only memory access policy."""

    allowed_write_actions = {"add", "edit", "update", "delete", "delete_all"}
    user_text = str(latest_user_text or "").lower()
    explicit_memory_browse = bool(
        re.search(
            r"\b(search|list|show|open|view)\b.{0,40}"
            r"\b(memories|memory|brain)\b",
            user_text,
        )
    )
    filtered_blocks = []
    filtered_calls = []
    dropped_memory_lookup = False
    for index, block in enumerate(tool_blocks):
        if block.tool_type != "manage_memory":
            filtered_blocks.append(block)
            if index < len(converted_calls):
                filtered_calls.append(converted_calls[index])
            continue
        action = ""
        try:
            arguments = json.loads(block.content or "{}")
            if isinstance(arguments, dict):
                action = str(arguments.get("action") or "").lower()
        except Exception:
            action = ""
        if action in {"list", "search", "view", "get", "read"}:
            if explicit_memory_browse:
                filtered_blocks.append(block)
                if index < len(converted_calls):
                    filtered_calls.append(converted_calls[index])
            else:
                dropped_memory_lookup = True
        elif (
            action in allowed_write_actions
            and re.search(
                r"\b(remember|forget|preference|prefer|save this about me|"
                r"update memory|delete memory)\b",
                user_text,
            )
        ):
            filtered_blocks.append(block)
            if index < len(converted_calls):
                filtered_calls.append(converted_calls[index])
        else:
            dropped_memory_lookup = True
    return QwenToolFilterResult(
        tool_blocks=filtered_blocks,
        converted_calls=filtered_calls,
        native_tool_calls=(
            filtered_calls if used_native else native_tool_calls
        ),
        dropped_memory_lookup=dropped_memory_lookup,
    )


def resolve_round_tool_calls(
    round_response: str,
    native_tool_calls: list,
    round_num: int,
    is_api_model: bool = False,
    allow_fenced_for_api: bool = False,
    recover_unknown: bool = True,
) -> ResolvedToolCalls:
    """Return aligned executable blocks and recoverable unknown native calls."""

    # Resolve through the compatibility package at call time. A few legacy
    # tests import agent_loop under temporary dependency stubs; lazy resolution
    # prevents those stubs from becoming permanently captured by this module.
    from src.agent_tools import (
        ToolBlock,
        function_call_to_tool_block,
        parse_tool_blocks,
    )
    tool_tags = TOOL_REGISTRY.accepted_names()

    used_native = False
    converted_calls = []
    unknown_calls: list[UnknownToolCall] = []
    incomplete_native_calls: list[str] = []
    if native_tool_calls:
        tool_blocks = []
        for tool_call in native_tool_calls:
            tool_name = tool_call.get("name", "")
            arguments = tool_call.get("arguments", "{}")
            if isinstance(arguments, str) and arguments.strip():
                try:
                    json.loads(arguments)
                except json.JSONDecodeError:
                    incomplete_native_calls.append(tool_name)
            block = function_call_to_tool_block(tool_name, arguments)
            if block:
                tool_blocks.append(block)
                converted_calls.append(tool_call)
                logger.info(
                    "  -> converted: %s -> %s",
                    tool_name,
                    block.tool_type,
                )
            else:
                is_unknown = bool(
                    tool_name
                    and not tool_name.startswith("mcp__")
                    and tool_name not in tool_tags
                )
                if is_unknown and recover_unknown:
                    suggestions = tuple(
                        get_close_matches(
                            tool_name,
                            sorted(tool_tags),
                            n=3,
                            cutoff=0.55,
                        )
                    )
                    try:
                        decoded_arguments = (
                            json.loads(arguments)
                            if isinstance(arguments, str)
                            else arguments
                        )
                    except (json.JSONDecodeError, TypeError):
                        decoded_arguments = arguments
                    unknown = UnknownToolCall(
                        name=tool_name,
                        arguments=decoded_arguments,
                        call_id=tool_call.get("id"),
                        suggestions=suggestions,
                    )
                    unknown_calls.append(unknown)
                    block = ToolBlock(
                        tool_name,
                        arguments if isinstance(arguments, str) else json.dumps(arguments),
                        {
                            "__unknown_native_tool__": True,
                            "requested_tool": tool_name,
                            "suggestions": list(suggestions),
                        },
                    )
                    tool_blocks.append(block)
                    converted_calls.append(tool_call)
                    logger.warning(
                        "  -> recoverable unknown native call: %s suggestions=%s",
                        tool_name,
                        suggestions,
                    )
                else:
                    logger.warning(
                        "  -> FAILED to convert native call: %s args=%s",
                        tool_name,
                        str(arguments)[:200],
                    )
        if tool_blocks:
            used_native = True
    fenced_call_unclosed = False
    if not used_native:
        _skip_fenced = is_api_model and not allow_fenced_for_api
        tool_blocks = parse_tool_blocks(
            round_response,
            skip_fenced=_skip_fenced,
        )
        if tool_blocks:
            logger.info(
                "Agent round %s: %s fenced tool block(s) detected",
                round_num,
                len(tool_blocks),
            )
        elif not _skip_fenced and _has_unclosed_tool_fence(round_response):
            fenced_call_unclosed = True
            logger.warning(
                "[agent] round %s unclosed fenced tool call detected "
                "(likely truncated mid-stream)",
                round_num,
            )

    response_preview = (
        round_response[:200].replace("\n", "\\n")
        if round_response
        else "(empty)"
    )
    logger.info(
        "Agent round %s summary: %s chars, %s native calls, "
        "%s tool blocks. Preview: %s",
        round_num,
        len(round_response),
        len(native_tool_calls),
        len(tool_blocks),
        response_preview,
    )
    return ResolvedToolCalls(
        tool_blocks=tool_blocks,
        used_native=used_native,
        converted_calls=converted_calls,
        unknown_calls=tuple(unknown_calls),
        incomplete_native_calls=tuple(incomplete_native_calls),
        fenced_call_unclosed=fenced_call_unclosed,
    )


def resolve_tool_blocks(
    round_response: str,
    native_tool_calls: list,
    round_num: int,
    is_api_model: bool = False,
    allow_fenced_for_api: bool = False,
):
    """Compatibility tuple for callers that have not migrated."""

    resolved = resolve_round_tool_calls(
        round_response,
        native_tool_calls,
        round_num,
        is_api_model=is_api_model,
        allow_fenced_for_api=allow_fenced_for_api,
        recover_unknown=False,
    )
    return (
        resolved.tool_blocks,
        resolved.used_native,
        resolved.converted_calls,
    )
