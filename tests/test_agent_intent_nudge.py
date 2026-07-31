from src.agent.contracts import SupervisorAction
from src.agent.supervision.intent_nudge import (
    evaluate_intent_without_action,
)


def test_short_unfinished_action_requests_a_tool_retry():
    decision = evaluate_intent_without_action(
        "Let me inspect the logs",
        nudge_count=0,
    )

    assert decision.action is SupervisorAction.RETRY_WITH_INSTRUCTION
    assert decision.reason == "intent_without_action"
    assert decision.metadata["nudges"] == 1
    assert "tail_serve_output" in decision.instruction


def test_long_answers_code_and_guide_only_turns_are_not_nudged():
    assert evaluate_intent_without_action(
        "Let me explain " + ("detail " * 100),
        nudge_count=0,
    ) is None
    assert evaluate_intent_without_action(
        "Let me run:\n```bash\necho ok\n```",
        nudge_count=0,
    ) is None
    assert evaluate_intent_without_action(
        "Let me inspect the logs",
        nudge_count=0,
        guide_only=True,
    ) is None


def test_nudge_cap_returns_explicit_finish_decision():
    decision = evaluate_intent_without_action(
        "I will run the test",
        nudge_count=2,
        max_nudges=2,
    )

    assert decision.action is SupervisorAction.FINISH
    assert decision.reason == "intent_without_action_nudge_cap"
    assert decision.metadata["nudges"] == 2
