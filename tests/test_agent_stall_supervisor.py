from types import SimpleNamespace

from src.agent.contracts import SupervisorAction
from src.agent.supervision.loop_breaker import StallSupervisor


def _block(tool="bash", content="echo ok"):
    return SimpleNamespace(tool_type=tool, content=content)


def test_distinct_calls_and_answer_text_reset_stall_progress():
    supervisor = StallSupervisor(stuck_threshold=2)

    assert supervisor.observe([_block(content="one")], real_text="") is None
    assert supervisor.observe([_block(content="two")], real_text="") is None
    assert supervisor.observe([_block(content="two")], real_text="answer") is None
    assert supervisor.stuck_rounds == 0


def test_repeated_empty_rounds_force_an_answer():
    supervisor = StallSupervisor(stuck_threshold=2)
    blocks = [_block()]

    assert supervisor.observe(blocks, real_text="") is None
    assert supervisor.observe(blocks, real_text="") is None
    decision = supervisor.observe(blocks, real_text="")

    assert decision.action is SupervisorAction.FORCE_ANSWER
    assert decision.reason == "loop_breaker_stall"
    assert "without new progress" in decision.metadata["detail"]


def test_identical_call_frequency_trips_runaway_backstop():
    supervisor = StallSupervisor(
        stuck_threshold=100,
        runaway_threshold=3,
    )
    blocks = [_block(tool="web_search", content='{"q":"same"}')]

    supervisor.observe(blocks, real_text="progress")
    supervisor.observe(blocks, real_text="progress")
    decision = supervisor.observe(blocks, real_text="progress")

    assert decision.metadata["runaway_tool"] == "web_search"
