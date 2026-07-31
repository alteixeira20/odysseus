"""Assembly of the ordered tool-aware system prompt."""

from collections.abc import Callable, Mapping
from typing import Optional, Sequence


def section_text(
    name: str,
    default: str,
    overrides: Mapping[str, object],
) -> str:
    """Return a non-empty user override or the shipped tool description."""

    value = overrides.get(name)
    return (
        value
        if isinstance(value, str) and value.strip()
        else default
    )


def compact_tool_line(name: str, section: str) -> str:
    """Build the legacy one-line fenced-tool usage hint."""

    text = (section or "").strip()
    if not text:
        return f"- `{name}`"
    if text.startswith("- "):
        return text
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    usage: list[str] = []
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            usage.append(line)
            in_fence = not in_fence
            if len(usage) >= 3:
                break
            continue
        if in_fence and len(usage) < 3:
            usage.append(line)
    if usage:
        return f"- `{name}` — " + " ".join(usage)
    return f"- `{name}` — " + lines[0][:160]


def assemble_prompt(
    tool_names: set[str],
    *,
    disabled_tools: set[str],
    compact: bool,
    tool_sections: Mapping[str, str],
    agent_preamble: str,
    agent_rules: str,
    api_agent_rules: str,
    resolve_section: Callable[[str, str], str],
    domain_rules_for_tools: Callable[[set[str]], list[str]],
) -> str:
    """Build the system prompt from explicit catalog and policy inputs."""

    included = tool_names - disabled_tools
    if compact:
        tool_lines = [f"- `{name}`" for name in sorted(included)]
        parts = [
            "You are an AI assistant with native tool/function calling. "
            "Only the tool schemas provided by the API are available for "
            "this turn. Use native tool calls when action is needed; do not "
            "write tool syntax or tool instructions in chat.",
            "## Available tools\n"
            + ("\n".join(tool_lines) if tool_lines else "none"),
            api_agent_rules,
        ]
        parts.extend(domain_rules_for_tools(included))
        return "\n\n".join(parts)

    parts = [agent_preamble]
    full_blocks: list[str] = []
    one_liners: list[str] = []
    for name, default_section in tool_sections.items():
        if name not in included:
            continue
        rendered = resolve_section(name, default_section)
        if rendered.startswith("```") or rendered.startswith("-"):
            if rendered.startswith("- "):
                one_liners.append(rendered)
            else:
                full_blocks.append(rendered)
    if full_blocks:
        parts.append("\n\n".join(full_blocks))
    if one_liners:
        parts.append("## Additional tools\n" + "\n".join(one_liners))
    parts.append(agent_rules)
    parts.extend(domain_rules_for_tools(included))
    return "\n\n".join(parts)


def assemble_prompt_messages(
    messages: Sequence[dict],
    *,
    agent_prompt: str,
    context_messages: Sequence[Optional[dict]] = (),
) -> list[dict]:
    """Insert trusted prompt and ordered contexts with legacy semantics."""

    agent_message = {"role": "system", "content": agent_prompt}
    insert_index = 0
    for index, message in enumerate(messages):
        if message.get("role") == "system":
            insert_index = index + 1
        else:
            break
    with_prompt = (
        list(messages[:insert_index])
        + [agent_message]
        + list(messages[insert_index:])
    )

    merged: list[dict] = []
    for message in with_prompt:
        if (
            message.get("role") == "system"
            and not message.get("_protected")
            and merged
            and merged[-1].get("role") == "system"
            and not merged[-1].get("_protected")
        ):
            merged[-1] = {
                "role": "system",
                "content": (
                    merged[-1]["content"]
                    + "\n\n"
                    + message["content"]
                ),
            }
        else:
            merged.append(message)

    latest_user_index = len(merged) - 1
    for index in range(len(merged) - 1, -1, -1):
        if merged[index].get("role") == "user":
            latest_user_index = index
            break
    for context_message in context_messages:
        if not context_message:
            continue
        merged.insert(latest_user_index, context_message)
        latest_user_index += 1
    return merged
