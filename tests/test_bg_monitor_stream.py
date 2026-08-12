from src import bg_monitor


def test_observe_chunk_ignores_non_string_deltas_and_tracks_tools():
    state = {"full": "", "tool_events": [], "round": 1}

    for chunk in (
        'data: {"delta": null}',
        'data: {"delta": ["bad"]}',
        'data: {"delta": "ok"}',
        'data: {"type": "agent_step", "round": 2}',
        'data: {"type": "tool_output", "tool": "shell", "output": "done"}',
        "data: [DONE]",
    ):
        bg_monitor._observe_chunk(chunk, state)

    assert state["full"] == "ok"
    assert state["tool_events"] == [{
        "round": 2,
        "tool": "shell",
        "command": None,
        "output": "done",
        "exit_code": None,
    }]
