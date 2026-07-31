from src import agent_loop
from src.agent.rounds.document_stream import (
    normalize_odysseus_qwen_text,
    normalize_stream_document_fences,
    strip_document_model_artifacts,
)
from src.tool_parsing import parse_tool_blocks

_normalize_stream_document_fences = normalize_stream_document_fences


def test_legacy_document_normalizers_are_compatibility_aliases():
    assert (
        agent_loop._normalize_stream_document_fences
        is normalize_stream_document_fences
    )
    assert (
        agent_loop._normalize_ody_qwen_text_artifacts
        is normalize_odysseus_qwen_text
    )
    assert (
        agent_loop._strip_doc_model_artifacts
        is strip_document_model_artifacts
    )


def test_model_artifacts_and_known_truncations_are_normalized():
    assert strip_document_model_artifacts(
        "<|im_start|>assistantHello<|im_end|>"
    ) == "Hello"
    assert normalize_odysseus_qwen_text(
        "The assistan read the lates documen."
    ) == "The assistant read the latest document."


def test_truncated_update_document_fence_is_executable():
    text = "```update_documen\n# Title\n\nSwedish body\n```"

    normalized = _normalize_stream_document_fences(text, "update_document")
    blocks = parse_tool_blocks(normalized)

    assert len(blocks) == 1
    assert blocks[0].tool_type == "update_document"
    assert "Swedish body" in blocks[0].content


def test_truncated_edit_document_fence_is_executable():
    text = (
        "```edit_documen\n"
        "<<<FIND>>>\nold\n<<<REPLACE>>>\nnew\n<<<END>>>\n"
        "```"
    )

    normalized = _normalize_stream_document_fences(text, "update_document")
    blocks = parse_tool_blocks(normalized)

    assert len(blocks) == 1
    assert blocks[0].tool_type == "edit_document"


def test_compact_truncated_edit_document_fence_is_executable():
    text = "```edi_documen\n<<FIND>old\n<<REPLACE>new\n<<END>```\n|end|"

    normalized = _normalize_stream_document_fences(text, "update_document")
    blocks = parse_tool_blocks(normalized)

    assert len(blocks) == 1
    assert blocks[0].tool_type == "edit_document"
    assert blocks[0].content == "<<<FIND>>>\nold\n<<<REPLACE>>>\nnew\n<<<END>>>"
