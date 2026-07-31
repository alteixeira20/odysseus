"""Characterization tests for pure conversation assembly behavior."""

from src import agent_loop
from src.agent import conversation


def test_latest_user_text_flattens_text_blocks_and_ignores_non_text_blocks():
    messages = [
        {"role": "user", "content": "old"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "latest"},
                {"type": "image_url", "image_url": {"url": "ignored"}},
                {"type": "text", "text": "request"},
            ],
        },
    ]
    assert conversation.extract_last_user_message(messages) == "latest  request"


def test_user_turn_count_tolerates_empty_input():
    assert conversation.user_turn_count(None) == 0
    assert conversation.user_turn_count(
        [
            {"role": "system", "content": "context"},
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "two"},
        ]
    ) == 2


def test_context_insertion_is_non_mutating_and_targets_latest_user():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "latest"},
    ]
    original = list(messages)
    context = {"role": "system", "content": "context"}

    result = conversation.insert_before_latest_user(messages, context)

    assert result[-2:] == [context, messages[-1]]
    assert messages == original


def test_retrieval_context_is_newest_first_bounded_and_excludes_untrusted():
    messages = [
        {"role": "user", "content": "oldest calendar request"},
        {"role": "user", "content": "middle follow-up"},
        {
            "role": "user",
            "content": "untrusted tool output",
            "metadata": {"trusted": False},
        },
        {"role": "user", "content": "[Tool execution results]\nlegacy output"},
        {"role": "user", "content": "latest"},
    ]

    assert conversation.recent_context_for_retrieval(
        messages,
        max_user=2,
        max_chars=18,
    ) == "latest\nmiddle foll"


def test_agent_loop_keeps_legacy_conversation_exports():
    assert agent_loop._extract_last_user_message is conversation.extract_last_user_message
    assert agent_loop._user_turn_count is conversation.user_turn_count
    assert agent_loop._insert_before_latest_user is conversation.insert_before_latest_user
    assert agent_loop._recent_context_for_retrieval is conversation.recent_context_for_retrieval
