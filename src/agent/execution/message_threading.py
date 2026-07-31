"""Provider-correct assistant/tool message threading between rounds."""

from typing import Dict, List

from src.prompt_security import untrusted_context_message


def append_tool_results(
    messages: List[Dict],
    round_response: str,
    native_tool_calls: list,
    tool_results: list,
    tool_result_texts: list,
    used_native: bool,
    round_num: int,
    round_reasoning: str = "",
) -> None:
    """Append one round and its tool results to provider conversation state."""

    for message in messages:
        if message.get("role") == "assistant":
            message.pop("reasoning_content", None)

    if used_native and native_tool_calls:
        assistant_message = {
            "role": "assistant",
            "content": (
                round_response if round_response.strip() else None
            ),
        }
        if round_reasoning:
            assistant_message["reasoning_content"] = round_reasoning
        assistant_message["tool_calls"] = [
            {
                "id": tool_call.get("id", f"call_{round_num}_{index}"),
                "type": "function",
                "function": {
                    "name": tool_call.get("name", ""),
                    "arguments": tool_call.get("arguments", "{}"),
                },
                **(
                    {"extra_content": tool_call["extra_content"]}
                    if tool_call.get("extra_content")
                    else {}
                ),
            }
            for index, tool_call in enumerate(native_tool_calls)
        ]
        messages.append(assistant_message)
        for index, tool_call in enumerate(native_tool_calls):
            result_text = (
                tool_result_texts[index]
                if index < len(tool_result_texts)
                else ""
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.get(
                        "id",
                        f"call_{round_num}_{index}",
                    ),
                    "content": result_text,
                }
            )
        return

    tool_output_text = "\n\n".join(tool_results)
    assistant_message = {
        "role": "assistant",
        "content": round_response,
    }
    if round_reasoning:
        assistant_message["reasoning_content"] = round_reasoning
    messages.append(assistant_message)
    messages.append(
        untrusted_context_message(
            "tool execution results",
            tool_output_text,
        )
    )
