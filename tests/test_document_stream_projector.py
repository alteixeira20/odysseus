from src.agent.rounds.document_stream import DocumentStreamProjector
from types import SimpleNamespace


def _payloads(events):
    return [dict(event.payload) for event in events]


def test_native_fragments_open_once_and_emit_growing_full_content():
    projector = DocumentStreamProjector()

    assert projector.consume_native_argument_delta('{"title":"My') == ()
    assert _payloads(
        projector.consume_native_argument_delta(
            ' \\"Doc\\"","language":"markdown","content":"first'
        )
    ) == [
        {
            "type": "doc_stream_open",
            "title": 'My "Doc"',
            "language": "markdown",
        },
        {"type": "doc_stream_delta", "content": "first"},
    ]
    assert _payloads(
        projector.consume_native_argument_delta("\\nsecond")
    ) == [
        {
            "type": "doc_stream_delta",
            "content": "first\nsecond",
        }
    ]
    assert projector.consume_native_argument_delta('"}') == ()


def test_native_partial_trailing_escape_is_repaired_without_regressing_length():
    projector = DocumentStreamProjector()

    first = projector.consume_native_argument_delta(
        '{"title":"T","content":"line\\'
    )
    second = projector.consume_native_argument_delta('nnext"}')

    assert _payloads(first)[-1] == {
        "type": "doc_stream_delta",
        "content": "line",
    }
    assert _payloads(second) == [
        {
            "type": "doc_stream_delta",
            "content": "line\nnext",
        }
    ]


def test_fenced_stream_is_suppressed_on_first_round_outside_create_mode():
    projector = DocumentStreamProjector()

    assert projector.consume_visible_text(
        "```create_document\nTitle\nmarkdown\nBody",
        round_number=1,
    ) == ()


def test_fenced_stream_opens_deltas_and_closes_with_monotonic_cursor():
    projector = DocumentStreamProjector()
    prefix = "```create_document\nTitle\nmarkdown\n"

    assert _payloads(
        projector.consume_visible_text(prefix + "One", round_number=2)
    ) == [
        {
            "type": "doc_stream_open",
            "title": "Title",
            "language": "markdown",
        },
        {"type": "doc_stream_delta", "content": "One"},
    ]
    assert _payloads(
        projector.consume_visible_text(prefix + "One two", round_number=2)
    ) == [
        {"type": "doc_stream_delta", "content": "One two"},
    ]
    assert (
        projector.consume_visible_text(
            prefix + "One two\n```",
            round_number=2,
        )
        == ()
    )
    assert projector.opened is False
    assert projector.scan_from > 0


def test_fenced_stream_can_project_multiple_blocks_in_one_round():
    projector = DocumentStreamProjector()
    first = "```create_document\nFirst\ntext\nA\n```"
    both = first + "\n```create_document\nSecond\ntext\nB"

    projector.consume_visible_text(first, round_number=2)
    events = projector.consume_visible_text(both, round_number=2)

    assert _payloads(events) == [
        {
            "type": "doc_stream_open",
            "title": "Second",
            "language": "text",
        },
        {"type": "doc_stream_delta", "content": "B"},
    ]


def test_odysseus_create_mode_accepts_truncated_marker_on_first_round():
    projector = DocumentStreamProjector(odysseus_create_mode=True)

    events = projector.consume_visible_text(
        "```documen\nDraft\nBody",
        round_number=1,
    )

    assert _payloads(events) == [
        {
            "type": "doc_stream_open",
            "title": "Draft",
            "language": "",
        },
        {"type": "doc_stream_delta", "content": "Body"},
    ]


def test_native_arguments_and_policy_block_suppress_fenced_projection():
    native_projector = DocumentStreamProjector(
        accumulated_arguments='{"query":"x"}'
    )
    blocked_projector = DocumentStreamProjector()
    response = "```create_document\nTitle\nBody"

    assert native_projector.consume_visible_text(
        response,
        round_number=2,
    ) == ()
    assert blocked_projector.consume_visible_text(
        response,
        round_number=2,
        create_document_blocked=True,
    ) == ()


def test_first_round_create_preview_preserves_frontend_fence_ownership():
    projector = DocumentStreamProjector()
    block = SimpleNamespace(
        tool_type="create_document",
        content="Draft\nmarkdown\nBody",
    )

    assert projector.preview_fenced_tool_blocks(
        [block],
        round_number=1,
        is_blocked=lambda name: False,
    ) == ()
    assert projector.opened is True


def test_later_create_and_update_previews_are_structured_events():
    create = SimpleNamespace(
        tool_type="create_document",
        content="Draft\nmarkdown\nBody",
    )
    update = SimpleNamespace(
        tool_type="update_document",
        content="Replacement",
    )

    assert _payloads(
        DocumentStreamProjector().preview_fenced_tool_blocks(
            [create],
            round_number=2,
            is_blocked=lambda name: False,
        )
    ) == [
        {
            "type": "doc_stream_open",
            "title": "Draft",
            "language": "markdown",
        },
        {"type": "doc_stream_delta", "content": "Body"},
    ]
    assert _payloads(
        DocumentStreamProjector().preview_fenced_tool_blocks(
            [update],
            round_number=2,
            is_blocked=lambda name: False,
        )
    ) == [
        {
            "type": "doc_stream_open",
            "title": "",
            "language": "",
        },
        {"type": "doc_stream_delta", "content": "Replacement"},
    ]


def test_blocked_fenced_preview_emits_nothing():
    block = SimpleNamespace(
        tool_type="create_document",
        content="Draft\nmarkdown\nBody",
    )
    projector = DocumentStreamProjector()

    assert projector.preview_fenced_tool_blocks(
        [block],
        round_number=2,
        is_blocked=lambda name: True,
    ) == ()
    assert projector.opened is False
