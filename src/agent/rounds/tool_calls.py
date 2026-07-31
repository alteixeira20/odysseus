"""Normalize native and textual provider tool calls for one round."""

from dataclasses import dataclass
from difflib import get_close_matches
import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class QwenToolFilterResult:
    tool_blocks: list
    converted_calls: list
    native_tool_calls: list
    dropped_memory_lookup: bool = False

    @property
    def requires_memory_answer_retry(self) -> bool:
        return self.dropped_memory_lookup and not self.tool_blocks


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
        TOOL_TAGS,
        ToolBlock,
        function_call_to_tool_block,
        parse_tool_blocks,
    )

    used_native = False
    converted_calls = []
    unknown_calls: list[UnknownToolCall] = []
    if native_tool_calls:
        tool_blocks = []
        for tool_call in native_tool_calls:
            tool_name = tool_call.get("name", "")
            arguments = tool_call.get("arguments", "{}")
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
                    and tool_name not in TOOL_TAGS
                )
                if is_unknown and recover_unknown:
                    suggestions = tuple(
                        get_close_matches(
                            tool_name,
                            sorted(TOOL_TAGS),
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
    if not used_native:
        tool_blocks = parse_tool_blocks(
            round_response,
            skip_fenced=(is_api_model and not allow_fenced_for_api),
        )
        if tool_blocks:
            logger.info(
                "Agent round %s: %s fenced tool block(s) detected",
                round_num,
                len(tool_blocks),
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
