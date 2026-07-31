from src.agent.rounds.document_stream import DocumentStreamProjector
from src.agent.rounds.provider_events import (
    DirectResponseAccumulator,
    ProviderRoundAccumulator,
)


def test_visible_reasoning_usage_and_model_events_accumulate_once():
    state = ProviderRoundAccumulator(
        requested_model="requested",
        actual_model="requested",
        round_number=1,
    )

    reasoning = state.consume({"delta": "think", "thinking": True})
    visible = state.consume({"delta": "answer"})
    usage = state.consume(
        {
            "type": "usage",
            "data": {
                "model": "actual",
                "input_tokens": 12,
                "output_tokens": 4,
                "gen_tps": 20,
            },
        }
    )
    model = state.consume({"type": "model_actual", "model": "actual"})

    assert reasoning.first_reasoning
    assert visible.first_visible
    assert state.reasoning == "think"
    assert state.text == "answer"
    assert usage.usage_input == 12
    assert usage.usage_output == 4
    assert state.actual_model == "actual"
    assert model.forward_data["requested_model"] == "requested"


def test_native_tool_calls_are_captured_when_document_stream_is_closed():
    state = ProviderRoundAccumulator(
        requested_model="m",
        actual_model="m",
        round_number=1,
    )
    calls = [{"id": "one", "name": "bash", "arguments": "{}"}]

    projection = state.consume({"type": "tool_calls", "calls": calls})

    assert projection.first_tool_calls
    assert state.native_tool_calls == calls


def test_blocked_native_delta_does_not_mutate_document_buffer():
    state = ProviderRoundAccumulator(
        requested_model="m",
        actual_model="m",
        round_number=1,
    )

    projection = state.consume(
        {
            "type": "tool_call_delta",
            "name": "create_document",
            "arg_delta": '{"title":"Blocked"}',
        },
        tool_call_blocked=True,
    )

    assert projection.substantive
    assert state.document_stream.accumulated_arguments == ""
    assert projection.document_events == ()


def test_native_document_open_preserves_legacy_later_tool_calls_quirk():
    state = ProviderRoundAccumulator(
        requested_model="m",
        actual_model="m",
        round_number=1,
    )
    opened = state.consume(
        {
            "type": "tool_call_delta",
            "name": "create_document",
            "arg_delta": '{"title":"Draft","content":"Body"}',
        }
    )
    state.consume(
        {
            "type": "tool_calls",
            "calls": [
                {
                    "id": "one",
                    "name": "create_document",
                    "arguments": "{}",
                }
            ],
        }
    )

    assert [event.kind for event in opened.document_events] == [
        "doc_stream_open",
        "doc_stream_delta",
    ]
    assert state.native_tool_calls == []


def test_qwen_visible_document_text_is_normalized_and_not_forwarded():
    state = ProviderRoundAccumulator(
        requested_model="qwen",
        actual_model="qwen",
        round_number=1,
        odysseus_qwen_finetune=True,
        document_stream=DocumentStreamProjector(
            odysseus_create_mode=True
        ),
    )

    projection = state.consume(
        {"delta": "```documen\nDraft\nThe lates tex"}
    )

    assert projection.forward_data is None
    assert state.text == "```document\nDraft\nThe latest text"
    assert [event.kind for event in projection.document_events] == [
        "doc_stream_open",
        "doc_stream_delta",
    ]


def test_direct_response_accumulator_tracks_usage_fallback_and_visible_text():
    state = DirectResponseAccumulator(
        requested_model="requested",
        actual_model="requested",
    )

    state.consume({"delta": "hidden", "thinking": True})
    visible = state.consume({"delta": "hello"})
    state.consume(
        {
            "type": "usage",
            "data": {
                "model": "actual",
                "input_tokens": 3,
                "output_tokens": 2,
            },
        }
    )

    assert visible.forward_raw is True
    assert state.text == "hello"
    assert state.input_tokens == 3
    assert state.output_tokens == 2
    assert state.actual_model == "actual"
