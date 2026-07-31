from src import agent_loop
from src.agent.supervision.verifier import (
    build_actions_snapshot,
    run_verifier_subagent,
)


def test_legacy_verifier_exports_are_aliases():
    assert agent_loop._build_actions_snapshot is build_actions_snapshot
    assert agent_loop._run_verifier_subagent is run_verifier_subagent


def test_action_snapshot_is_bounded_and_records_failures():
    snapshot = build_actions_snapshot(
        [
            {
                "tool": "bash",
                "command": "false",
                "output": "failed",
                "exit_code": 1,
            }
        ],
        limit=80,
    )

    assert "[bash] false (exit 1)" in snapshot
    assert "-> failed" in snapshot
    assert len(snapshot) <= 80
