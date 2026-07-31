from src.agent.providers.adapters.odysseus_qwen import (
    looks_like_destructive_request,
    looks_like_memory_identity_turn,
    looks_like_notes_calendar_followup,
    looks_like_notes_turn,
    looks_like_success_claim,
    terminal_tool_summary,
)


def test_legacy_agent_loop_names_are_identity_preserving_aliases():
    import src.agent_loop as legacy

    assert legacy._looks_like_notes_turn is looks_like_notes_turn
    assert (
        legacy._looks_like_notes_calendar_followup
        is looks_like_notes_calendar_followup
    )
    assert (
        legacy._looks_like_memory_identity_turn
        is looks_like_memory_identity_turn
    )
    assert (
        legacy._looks_like_destructive_request
        is looks_like_destructive_request
    )
    assert legacy._looks_like_success_claim is looks_like_success_claim
    assert legacy._ody_qwen_terminal_tool_summary is terminal_tool_summary


def test_notes_and_memory_routing_guards_preserve_specialized_turns():
    assert looks_like_notes_turn("Jot down a reminder to buy milk")
    assert looks_like_notes_turn("pick up bread")
    assert not looks_like_notes_turn("schedule a calendar meeting")
    assert looks_like_notes_calendar_followup("now delete that event")
    assert looks_like_memory_identity_turn("What do you know about me?")
    assert looks_like_memory_identity_turn("hwho am i")
    assert not looks_like_memory_identity_turn("Who is Odysseus?")


def test_terminal_tool_summary_uses_concrete_namespaced_tool_name():
    summary = terminal_tool_summary(
        {
            "tool": "mcp",
            "desc": "Executed mcp__email__read_email",
            "command": "{}",
            "output": (
                "**Subject:** Status\n"
                "**From:** teammate@example.test\n"
                "**Date:** Today\n"
                "**UID:** 42\n"
                "---\n"
                "All green."
            ),
        }
    )

    assert summary == (
        "Email: Status\n"
        "From: teammate@example.test\n"
        "Date: Today\n"
        "UID: 42\n\n"
        "All green."
    )


def test_destructive_success_guard_requires_both_independent_signals():
    assert looks_like_destructive_request("Delete that email")
    assert not looks_like_destructive_request("Show that email")
    assert looks_like_success_claim("Done, it was deleted")
    assert not looks_like_success_claim("I could not delete it")
