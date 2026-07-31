import hashlib
import json
from types import SimpleNamespace

import src.agent_loop as legacy
from src.agent.prompting.odysseus_qwen import (
    minimal_document_messages,
    minimal_general_messages,
    minimal_notes_messages,
    minimal_recent_tool_context_message,
    minimal_saved_memory_message,
)


def _messages():
    return [
        {"role": "user", "content": "Earlier question"},
        {
            "role": "assistant",
            "content": "Earlier answer",
            "metadata": {
                "tool_events": [
                    {
                        "tool": "mcp",
                        "desc": "Executed mcp__email__read_email",
                        "command": '{"uid":"42"}',
                        "output": "Message body",
                    }
                ]
            },
        },
        {
            "role": "user",
            "content": (
                "Source: saved memory: profile\n"
                "Core facts about the user:\n"
                "- Lives in Lisbon\n"
                "- Prefers concise replies"
            ),
            "metadata": {"source": "saved memory: profile"},
        },
        {"role": "user", "content": "Latest request"},
    ]


def _snapshot(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return len(raw), hashlib.sha256(raw.encode()).hexdigest()


def test_legacy_minimal_prompt_names_remain_identity_aliases():
    assert legacy._minimal_saved_memory_message is minimal_saved_memory_message
    assert (
        legacy._minimal_recent_notes_tool_context_message
        is minimal_recent_tool_context_message
    )
    assert legacy._minimal_odysseus_doc_messages is minimal_document_messages
    assert legacy._minimal_odysseus_notes_messages is minimal_notes_messages
    assert (
        legacy._minimal_odysseus_general_messages
        is minimal_general_messages
    )


def test_minimal_qwen_prompt_packs_match_characterized_snapshots():
    messages = _messages()
    document = SimpleNamespace(
        title="Draft",
        language="markdown",
        current_content="One\nTwo",
    )
    cases = {
        "memory": (
            minimal_saved_memory_message(messages),
            (
                305,
                "b270192df505befedf135269de356ae20"
                "cef5247a135f52f98c76b109c03b51d",
            ),
        ),
        "recent": (
            minimal_recent_tool_context_message(messages),
            (
                526,
                "7a21c828c3821e2e83df1983ee4be039"
                "3ec65d441201e09c96528547643c9934",
            ),
        ),
        "doc_create": (
            minimal_document_messages(messages, document, True),
            (
                842,
                "8c5daa93ec478f40827a1d160d1e1856"
                "e2c3c2387c0b179fce547f5f4e0ac236",
            ),
        ),
        "doc_edit": (
            minimal_document_messages(messages, document, False),
            (
                2194,
                "6e4f4a2c48ed1ecbe174234ae39714fc"
                "be5bb70b4eb684ff9fc496f1f20e2832",
            ),
        ),
        "notes": (
            minimal_notes_messages(messages),
            (
                1603,
                "991a441f27fe66ae1a6c364bc02f60823"
                "6c6ad0fe129fae1efb80b5d3b6a8c15",
            ),
        ),
        "general": (
            minimal_general_messages(messages, True),
            (
                1464,
                "8a115c2582fb5479bb4749f86cfdb06fc"
                "8bb5a070bea3be8f1b0caba9ce874c3",
            ),
        ),
    }

    assert {
        name: _snapshot(value)
        for name, (value, expected) in cases.items()
    } == {
        name: expected
        for name, (value, expected) in cases.items()
    }
