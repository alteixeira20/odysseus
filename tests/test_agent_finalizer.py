from src.agent.supervision.finalizer import (
    select_deterministic_tool_summary,
)


def _event(tool, command, output):
    return {
        "tool": tool,
        "command": command,
        "output": output,
    }


def test_latest_supported_tool_result_wins():
    events = [
        _event(
            "manage_tasks",
            '{"action":"list"}',
            "AI: Older task summary",
        ),
        _event(
            "manage_notes",
            '{"action":"list"}',
            "- [n1] **Latest note**",
        ),
    ]

    summary = select_deterministic_tool_summary(events)

    assert summary.matched is True
    assert summary.text == "Here are your notes (1):\n- Latest note"


def test_supported_empty_result_stops_at_latest_matching_tool():
    events = [
        _event(
            "manage_tasks",
            '{"action":"list"}',
            "AI: Older task summary",
        ),
        _event("manage_notes", '{"action":"list"}', "unparseable"),
    ]

    summary = select_deterministic_tool_summary(events)

    assert summary.matched is True
    assert summary.text == ""


def test_unrelated_tool_results_do_not_replace_final_response():
    summary = select_deterministic_tool_summary(
        [_event("bash", "printf ok", "ok")]
    )

    assert summary.matched is False
    assert summary.text == ""
