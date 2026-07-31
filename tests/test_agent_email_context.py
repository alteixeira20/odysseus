from src import agent_loop
from src.agent.prompting.contexts.email import (
    compact_email_draft_context,
)


def test_legacy_email_compactor_is_context_provider_alias():
    assert (
        agent_loop._compact_email_draft_context
        is compact_email_draft_context
    )


def test_email_compactor_preserves_headers_and_bounds_quoted_history():
    raw = (
        "To: person@example.test\nSubject: Re: Hi\n---\n"
        "My reply\n\n---------- Previous message ----------\n"
        + ("old " * 500)
    )

    compact = compact_email_draft_context(
        raw,
        max_history_chars=80,
    )

    assert compact.startswith(
        "To: person@example.test\nSubject: Re: Hi\n---\nMy reply"
    )
    assert "QUOTED HISTORY EXCERPT FOR CONTEXT ONLY" in compact
    assert "quoted history truncated" in compact
