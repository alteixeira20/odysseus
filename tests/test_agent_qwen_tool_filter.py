from src.agent.rounds.tool_calls import filter_odysseus_qwen_calls
from src.agent_tools import ToolBlock


def test_implicit_memory_lookup_is_removed_and_requests_answer_retry():
    call = {"name": "manage_memory", "arguments": '{"action":"list"}'}

    result = filter_odysseus_qwen_calls(
        [ToolBlock("manage_memory", '{"action":"list"}')],
        [call],
        [call],
        used_native=True,
        latest_user_text="what is my name?",
    )

    assert result.tool_blocks == []
    assert result.converted_calls == []
    assert result.native_tool_calls == []
    assert result.dropped_memory_lookup is True
    assert result.requires_memory_answer_retry is True


def test_explicit_memory_write_is_retained_with_aligned_native_call():
    call = {"name": "manage_memory", "arguments": '{"action":"add"}'}

    result = filter_odysseus_qwen_calls(
        [ToolBlock("manage_memory", '{"action":"add"}')],
        [call],
        [call],
        used_native=True,
        latest_user_text="remember that I prefer concise answers",
    )

    assert [block.tool_type for block in result.tool_blocks] == [
        "manage_memory"
    ]
    assert result.converted_calls == [call]
    assert result.native_tool_calls == [call]
    assert result.dropped_memory_lookup is False


def test_explicit_memory_browse_is_retained():
    call = {"name": "manage_memory", "arguments": '{"action":"list"}'}

    result = filter_odysseus_qwen_calls(
        [ToolBlock("manage_memory", '{"action":"list"}')],
        [call],
        [call],
        used_native=True,
        latest_user_text="show my saved memories",
    )

    assert [block.tool_type for block in result.tool_blocks] == [
        "manage_memory"
    ]
    assert result.converted_calls == [call]
    assert result.native_tool_calls == [call]
    assert result.dropped_memory_lookup is False
    assert result.requires_memory_answer_retry is False


def test_non_memory_calls_keep_their_converted_call_alignment():
    memory_call = {
        "name": "manage_memory",
        "arguments": '{"action":"list"}',
    }
    notes_call = {
        "name": "manage_notes",
        "arguments": '{"action":"list"}',
    }

    result = filter_odysseus_qwen_calls(
        [
            ToolBlock("manage_memory", '{"action":"list"}'),
            ToolBlock("manage_notes", '{"action":"list"}'),
        ],
        [memory_call, notes_call],
        [memory_call, notes_call],
        used_native=True,
        latest_user_text="show my notes",
    )

    assert [block.tool_type for block in result.tool_blocks] == [
        "manage_notes"
    ]
    assert result.converted_calls == [notes_call]
    assert result.native_tool_calls == [notes_call]
    assert result.requires_memory_answer_retry is False
