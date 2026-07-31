from src.agent.execution.result_adapters import (
    build_tool_output_payload,
    document_result_payload,
    extract_web_sources,
    generated_image_payload,
    project_tool_result,
    render_tool_output,
    resolved_tool_event_name,
)


def test_web_sources_are_extracted_and_removed_from_visible_output():
    result = {
        "output": (
            "Useful result\n<!-- SOURCES:"
            '[{"title":"Primary","url":"https://example.test"}] -->'
        )
    }

    sources = extract_web_sources(result)

    assert sources == [
        {"title": "Primary", "url": "https://example.test"}
    ]
    assert result["output"] == "Useful result"


def test_timeout_rendering_preserves_partial_stdout_and_stderr():
    output = render_tool_output(
        {
            "timed_out": True,
            "error": "Timed out after 3 seconds.",
            "stdout": "before",
            "stderr": "warning",
        },
        is_document_tool=False,
    )

    assert output == (
        "Timed out after 3 seconds.\n\n"
        "Partial stdout:\nbefore\n\n"
        "Partial stderr:\nwarning"
    )


def test_document_projection_and_card_summary_are_separate():
    result = {
        "action": "edit",
        "doc_id": 7,
        "content": "new",
        "version": 3,
        "title": "Draft",
        "language": "markdown",
        "applied": 2,
    }

    assert document_result_payload(result) == {
        "type": "doc_update",
        "doc_id": 7,
        "content": "new",
        "version": 3,
        "title": "Draft",
        "language": "markdown",
    }
    assert render_tool_output(
        result,
        is_document_tool=True,
    ) == 'Document edited: "Draft" (v3, 2 edit(s))'


def test_tool_output_payload_carries_lifecycle_ui_and_artifact_fields():
    payload = build_tool_output_payload(
        tool_name="bash",
        command="sleep 30",
        output="Timed out",
        result={
            "exit_code": 124,
            "completion_state": "timed_out",
            "timed_out": True,
            "timeout_seconds": 3,
            "ui_event": "open_panel",
            "panel": "terminal",
            "diff": "patch",
        },
        invocation_id="fallback-id",
        is_document_tool=False,
    )

    assert payload == {
        "type": "tool_output",
        "tool": "bash",
        "command": "sleep 30",
        "output": "Timed out",
        "exit_code": 124,
        "invocation_id": "fallback-id",
        "completion_state": "timed_out",
        "timed_out": True,
        "timeout_seconds": 3,
        "ui_event": "open_panel",
        "panel": "terminal",
        "diff": "patch",
    }


def test_generated_image_payload_is_additive_and_optional():
    assert generated_image_payload({}) is None
    assert generated_image_payload(
        {"image_url": "data:image/png;base64,abc", "image_id": "one"}
    ) == {
        "type": "generated_image",
        "url": "data:image/png;base64,abc",
        "image_url": "data:image/png;base64,abc",
        "image_id": "one",
    }


def test_mcp_event_name_is_recovered_from_persisted_event_details():
    assert resolved_tool_event_name(
        {
            "tool": "mcp",
            "desc": "Calling mcp__email__read_email",
        }
    ) == "mcp__email__read_email"
    assert resolved_tool_event_name(
        {"tool": "bash", "command": "printf ok"}
    ) == "bash"
    assert resolved_tool_event_name({"tool": "mcp"}) == "mcp"


def test_result_projection_preserves_ask_plan_output_image_event_order():
    result = {
        "ask_user": {
            "question": "Choose one",
            "options": ["A", "B"],
        },
        "plan_update": {"steps": []},
        "ui_event": "open_panel",
        "panel": "documents",
        "output": "ready",
        "image_url": "data:image/png;base64,abc",
        "image_id": "image-one",
        "exit_code": 0,
    }

    projection = project_tool_result(
        tool_name="ask_user",
        command='{"question":"Choose one"}',
        description="Asked",
        result=result,
        invocation_id="call-one",
        round_number=2,
        current_response="Earlier",
    )

    assert [
        event.get("type", "delta")
        for event in projection.before_summary_events
    ] == [
        "ui_control",
        "delta",
        "plan_update",
        "tool_output",
        "generated_image",
    ]
    assert [
        event["type"] for event in projection.after_summary_events
    ] == ["ask_user"]
    assert projection.question_delta == "\n\nChoose one"
    assert projection.awaiting_user is True
    assert projection.tool_event["ask_user"] == result["ask_user"]


def test_result_projection_cleans_web_sources_before_tool_output():
    result = {
        "output": (
            "Visible\n<!-- SOURCES:"
            '[{"title":"Primary","url":"https://example.test"}] -->'
        ),
        "exit_code": 0,
    }

    projection = project_tool_result(
        tool_name="web_search",
        command="query",
        description="Searched",
        result=result,
        invocation_id="call-web",
        round_number=1,
        current_response="",
    )

    assert projection.before_summary_events[0] == {
        "type": "web_sources",
        "data": [
            {
                "title": "Primary",
                "url": "https://example.test",
            }
        ],
    }
    assert projection.before_summary_events[1]["type"] == "tool_output"
    assert projection.before_summary_events[1]["output"] == "Visible"
    assert result["output"] == "Visible"


def test_result_projection_preserves_duplicate_document_update_contract():
    projection = project_tool_result(
        tool_name="create_document",
        command="Draft",
        description="Created",
        result={
            "action": "create",
            "doc_id": 7,
            "content": "Body",
            "version": 1,
            "title": "Draft",
            "language": "markdown",
            "exit_code": 0,
        },
        invocation_id="call-doc",
        round_number=1,
        current_response="",
    )

    assert [
        event["type"] for event in projection.before_summary_events
    ] == ["doc_update", "tool_output"]
    assert [
        event["type"] for event in projection.after_summary_events
    ] == ["doc_update"]
    assert projection.tool_event["doc_id"] == 7


def test_result_projection_builds_persisted_note_and_research_links():
    projection = project_tool_result(
        tool_name="manage_notes",
        command='{"action":"add"}',
        description="Added note",
        result={
            "note_id": 9,
            "note_title": "Packing",
            "research_session_id": "research-one",
            "output": "Done",
            "diff": "patch",
            "exit_code": 0,
        },
        invocation_id="call-note",
        round_number=3,
        current_response="",
    )

    assert [
        event["delta"] for event in projection.after_summary_events
    ] == [
        "\n\n[Open in Deep Research](#research-research-one)\n",
        "\n\n[View note: Packing](#note-9)\n",
    ]
    assert projection.note_anchor.endswith("(#note-9)\n")
    assert projection.tool_event["diff"] == "patch"
