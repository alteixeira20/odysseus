"""`update_plan` — the agent writes back to the active plan (tick done / revise).

Legacy wire-compatible adapter over PlanService (src/agent/planning/service.py):
`execute_tool_block` still returns a `plan_update` payload the agent loop turns
into a `plan_update` SSE event, but the checklist is now parsed into structured
steps and persisted through the same canonical, versioned backend
`todowrite`/`manage_plan` use, then regenerated from those steps for the SSE
payload. Does not end the turn.
"""
import asyncio
import json

import pytest

from src.agent.planning.service import PLAN_SERVICE
from src.agent_tools import ToolBlock, TOOL_TAGS  # import first to avoid circular
from src.tool_execution import execute_tool_block
from src.tool_index import ALWAYS_AVAILABLE, BUILTIN_TOOL_DESCRIPTIONS
from src.tool_security import is_public_blocked_tool


@pytest.fixture(autouse=True)
def _isolated_plan_root(tmp_path, monkeypatch):
    # PLAN_SERVICE is a process-wide singleton; point it at a temp root so
    # these tests never touch the real data path.
    monkeypatch.setattr(PLAN_SERVICE, "_root", tmp_path / "agent_plans")


def _run(content):
    return asyncio.run(execute_tool_block(ToolBlock("update_plan", content)))


def test_valid_plan_returns_marker_and_counts():
    plan = "- [x] step one\n- [ ] step two\n- [ ] step three"
    desc, result = _run(json.dumps({"plan": plan}))
    assert result.get("exit_code") == 0
    assert result["plan_update"]["plan"] == plan
    assert "1/3" in result["output"]   # 1 done of 3


def test_plain_string_accepted():
    plan = "- [ ] a\n- [x] b"
    _, result = _run(plan)
    assert result["plan_update"]["plan"] == plan


def test_empty_rejected():
    _, result = _run(json.dumps({"plan": "   "}))
    assert "error" in result and result.get("exit_code") == 1


def test_registered_everywhere():
    assert "update_plan" in TOOL_TAGS
    assert "update_plan" in ALWAYS_AVAILABLE
    assert "update_plan" in BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    assert "update_plan" in {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    # Not admin/public-gated — any user can drive their own plan.
    assert is_public_blocked_tool("update_plan") is False


def test_manage_plan_registered_everywhere():
    assert "manage_plan" in TOOL_TAGS
    assert "manage_plan" in ALWAYS_AVAILABLE
    assert "manage_plan" in BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    assert "manage_plan" in {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert is_public_blocked_tool("manage_plan") is False


def test_update_plan_persists_through_plan_service():
    plan = "- [x] step one\n- [ ] step two"
    asyncio.run(
        execute_tool_block(
            ToolBlock("update_plan", json.dumps({"plan": plan})),
            session_id="s1",
        )
    )

    stored = asyncio.run(PLAN_SERVICE.read(owner_id=None, session_id="s1"))
    assert stored is not None
    assert [s.content for s in stored.steps] == ["step one", "step two"]
    assert stored.steps[0].status.value == "completed"
    assert stored.steps[1].status.value == "pending"


def test_update_plan_and_todowrite_share_one_plan():
    # Both legacy tools must write through the same canonical plan for a
    # given owner+session — not independent state.
    from src.agent_tools import ToolBlock as _TB
    from src.tool_execution import execute_tool_block as _exec

    asyncio.run(
        _exec(
            _TB("update_plan", json.dumps({"plan": "- [ ] from update_plan"})),
            session_id="shared",
        )
    )
    asyncio.run(
        _exec(
            _TB(
                "todowrite",
                json.dumps(
                    {"todos": [{"content": "from todowrite", "status": "pending"}]}
                ),
            ),
            session_id="shared",
        )
    )

    stored = asyncio.run(PLAN_SERVICE.read(owner_id=None, session_id="shared"))
    # todowrite's replace() call is the most recent write and wins — same
    # replace-the-whole-list semantics as calling either tool twice. Two
    # writes to a freshly created plan means version advanced twice past
    # its initial value of 1.
    assert [s.content for s in stored.steps] == ["from todowrite"]
    assert stored.version == 3
