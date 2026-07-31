"""`manage_plan` — the structured planning tool (src/agent_tools/planning_tools.py),
the third caller of PlanService alongside the legacy todowrite/update_plan
adapters. Covers action dispatch, partial updates, and the conflict payload.
"""

import asyncio
import json

import pytest

from src.agent.planning.service import PLAN_SERVICE
from src.agent_tools import ToolBlock
from src.tool_execution import execute_tool_block


@pytest.fixture(autouse=True)
def _isolated_plan_root(tmp_path, monkeypatch):
    monkeypatch.setattr(PLAN_SERVICE, "_root", tmp_path / "agent_plans")


def _call(args, session_id="s1", owner=None):
    return asyncio.run(
        execute_tool_block(
            ToolBlock("manage_plan", json.dumps(args)),
            session_id=session_id,
            owner=owner,
        )
    )


def test_unknown_action_rejected():
    _, result = _call({"action": "bogus"})
    assert result["exit_code"] == 1
    assert "unknown action" in result["error"]


def test_create_then_read():
    _call({"action": "create", "title": "My plan", "steps": [{"content": "a"}]})
    _, result = _call({"action": "read"})
    assert result["plan"]["title"] == "My plan"
    assert result["plan"]["steps"][0]["content"] == "a"


def test_read_with_no_plan_yet_is_not_an_error():
    _, result = _call({"action": "read"})
    assert result["exit_code"] == 0
    assert result["plan"] is None


def test_add_step_then_complete_it():
    _call({"action": "create", "steps": [{"content": "a"}]})
    _, r1 = _call({"action": "add_step", "content": "b", "priority": "high"})
    step_id = r1["plan"]["steps"][1]["id"]

    _, r2 = _call({"action": "complete_step", "step_id": step_id, "evidence": {"note": "done"}})
    completed = next(s for s in r2["plan"]["steps"] if s["id"] == step_id)
    assert completed["status"] == "completed"


def test_add_step_missing_content_is_rejected():
    _call({"action": "create", "steps": []})
    _, result = _call({"action": "add_step", "content": ""})
    assert result["exit_code"] == 1
    assert "content" in result["error"]


def test_block_step_records_notes():
    _call({"action": "create", "steps": [{"content": "a"}]})
    _, r1 = _call({"action": "read"})
    step_id = r1["plan"]["steps"][0]["id"]
    _, r2 = _call({"action": "block_step", "step_id": step_id, "notes": "waiting on deploy"})
    step = r2["plan"]["steps"][0]
    assert step["status"] == "blocked"
    assert step["notes"] == "waiting on deploy"


def test_version_conflict_returns_latest_and_is_retryable():
    _call({"action": "create", "steps": [{"content": "a"}]})
    _, r1 = _call({"action": "read"})
    stale_version = r1["plan"]["version"]
    _call({"action": "add_step", "content": "b"})  # bumps version past stale_version

    _, conflict = _call(
        {"action": "add_step", "content": "c", "expected_version": stale_version}
    )
    assert conflict["exit_code"] == 1
    assert conflict["conflict"] is True
    assert conflict["latest_version"] > stale_version
    assert [s["content"] for s in conflict["latest_plan"]["steps"]] == ["a", "b"]

    # Retrying with the now-current version succeeds.
    _, retried = _call(
        {"action": "add_step", "content": "c", "expected_version": conflict["latest_version"]}
    )
    assert retried["exit_code"] == 0
    assert [s["content"] for s in retried["plan"]["steps"]] == ["a", "b", "c"]


def test_reorder_requires_step_ids():
    _call({"action": "create", "steps": [{"content": "a"}]})
    _, result = _call({"action": "reorder", "step_ids": []})
    assert result["exit_code"] == 1


def test_operating_on_missing_step_id_errors_cleanly():
    _call({"action": "create", "steps": [{"content": "a"}]})
    _, result = _call({"action": "complete_step", "step_id": "does-not-exist"})
    assert result["exit_code"] == 1
    assert "does-not-exist" in result["error"]


def test_todowrite_manage_plan_and_update_plan_share_state():
    # All three surfaces must read/write the exact same plan.
    from src.agent_tools import ToolBlock as _TB
    from src.tool_execution import execute_tool_block as _exec

    asyncio.run(
        _exec(
            _TB("todowrite", json.dumps({"todos": [{"content": "from todowrite", "status": "pending"}]})),
            session_id="shared2",
        )
    )
    _, r = _call({"action": "read"}, session_id="shared2")
    assert r["plan"]["steps"][0]["content"] == "from todowrite"

    asyncio.run(
        _exec(
            _TB("update_plan", json.dumps({"plan": "- [ ] from update_plan"})),
            session_id="shared2",
        )
    )
    _, r2 = _call({"action": "read"}, session_id="shared2")
    assert r2["plan"]["steps"][0]["content"] == "from update_plan"
