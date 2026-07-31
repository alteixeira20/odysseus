from types import SimpleNamespace

from src import agent_loop
from src.agent.routing.classifier import (
    EXPLICIT_CONTINUATION_RE,
    RoutingDecision,
    classify_agent_request,
    classify_routing_decision,
)
from src.agent.routing.context_targets import (
    is_email_document,
    turn_targets_active_document,
)


def test_typed_routing_decision_preserves_legacy_projection():
    decision = classify_routing_decision(
        [],
        "Search the web for current weather",
    )

    assert isinstance(decision, RoutingDecision)
    assert decision.domains == frozenset({"web"})
    assert "domain:web" in decision.decision_reasons
    assert classify_agent_request(
        [],
        "Search the web for current weather",
    ) == decision.as_legacy_dict()


def test_legacy_routing_names_are_identity_preserving_aliases():
    assert agent_loop._classify_agent_request is classify_agent_request
    assert (
        agent_loop._EXPLICIT_CONTINUATION_RE
        is EXPLICIT_CONTINUATION_RE
    )
    assert (
        agent_loop._turn_targets_active_document
        is turn_targets_active_document
    )
    assert agent_loop._is_email_document_obj is is_email_document


def test_assistant_followup_inherits_prior_domain_context():
    messages = [
        {"role": "user", "content": "Create a task"},
        {
            "role": "assistant",
            "content": "What would you like on your to-do list?",
        },
        {"role": "user", "content": "buy milk"},
    ]

    decision = classify_routing_decision(messages, "buy milk")

    assert decision.continuation is True
    assert "notes_calendar_tasks" in decision.domains


def test_open_email_document_is_targeted_only_for_relevant_turn():
    document = SimpleNamespace(
        title="New Email",
        language="email",
        current_content="To: a@example.test\nSubject: Hi\n---\nBody",
    )

    assert is_email_document(document)
    assert turn_targets_active_document(
        {"domains": set()},
        "make it more formal",
        document,
    )
    assert not turn_targets_active_document(
        {"domains": {"web"}},
        "search current weather",
        document,
    )
