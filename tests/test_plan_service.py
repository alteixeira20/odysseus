"""Coverage for PlanService (src/agent/planning/service.py) — the single
canonical planning backend behind todowrite/update_plan/manage_plan.
"""

import asyncio
import json

import pytest

from src.agent.planning.service import (
    Plan,
    PlanNotFound,
    PlanService,
    PlanStepNotFound,
    PlanStepStatus,
    PlanVersionConflict,
    markdown_checklist_to_steps,
    steps_to_markdown_checklist,
)


@pytest.fixture
def svc(tmp_path):
    return PlanService(root=str(tmp_path / "agent_plans"))


def _run(coro):
    return asyncio.run(coro)


def test_create_and_read_round_trip(svc):
    async def _go():
        created = await svc.create(
            owner_id="u1", session_id="s1", title="T", summary="S",
            steps=[{"content": "step a"}],
        )
        read_back = await svc.read(owner_id="u1", session_id="s1")
        assert read_back.id == created.id
        assert read_back.title == "T"
        assert [s.content for s in read_back.steps] == ["step a"]

    _run(_go())


def test_read_missing_plan_returns_none(svc):
    assert _run(svc.read(owner_id="u1", session_id="nope")) is None


def test_step_ids_are_stable_across_reads(svc):
    async def _go():
        plan = await svc.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}])
        step_id = plan.steps[0].id
        again = await svc.read(owner_id="u1", session_id="s1")
        assert again.steps[0].id == step_id

    _run(_go())


def test_partial_add_step_does_not_require_full_plan(svc):
    async def _go():
        await svc.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}])
        plan = await svc.add_step(owner_id="u1", session_id="s1", content="b")
        assert [s.content for s in plan.steps] == ["a", "b"]

    _run(_go())


def test_update_step_partial_fields_only(svc):
    async def _go():
        plan = await svc.create(
            owner_id="u1", session_id="s1",
            steps=[{"content": "a", "priority": "low"}],
        )
        step_id = plan.steps[0].id
        updated = await svc.update_step(
            owner_id="u1", session_id="s1", step_id=step_id, status="in_progress"
        )
        step = updated.step(step_id)
        assert step.status is PlanStepStatus.IN_PROGRESS
        assert step.priority.value == "low"  # untouched field survives
        assert step.content == "a"  # untouched field survives

    _run(_go())


def test_complete_step_sets_completed_at_and_evidence(svc):
    async def _go():
        plan = await svc.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}])
        step_id = plan.steps[0].id
        updated = await svc.complete_step(
            owner_id="u1", session_id="s1", step_id=step_id,
            evidence={"test": "pytest tests/x.py"},
        )
        step = updated.step(step_id)
        assert step.status is PlanStepStatus.COMPLETED
        assert step.completed_at is not None
        assert step.evidence["test"] == "pytest tests/x.py"

    _run(_go())


def test_block_and_skip_steps(svc):
    async def _go():
        plan = await svc.create(
            owner_id="u1", session_id="s1",
            steps=[{"content": "a"}, {"content": "b"}],
        )
        a_id, b_id = plan.steps[0].id, plan.steps[1].id
        plan = await svc.block_step(owner_id="u1", session_id="s1", step_id=a_id, notes="waiting on X")
        plan = await svc.skip_step(owner_id="u1", session_id="s1", step_id=b_id, notes="no longer needed")
        assert plan.step(a_id).status is PlanStepStatus.BLOCKED
        assert plan.step(a_id).notes == "waiting on X"
        assert plan.step(b_id).status is PlanStepStatus.SKIPPED

    _run(_go())


def test_remove_step(svc):
    async def _go():
        plan = await svc.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}, {"content": "b"}])
        a_id = plan.steps[0].id
        plan = await svc.remove_step(owner_id="u1", session_id="s1", step_id=a_id)
        assert [s.content for s in plan.steps] == ["b"]

    _run(_go())


def test_remove_missing_step_raises(svc):
    async def _go():
        await svc.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}])
        with pytest.raises(PlanStepNotFound):
            await svc.remove_step(owner_id="u1", session_id="s1", step_id="not-real")

    _run(_go())


def test_reorder_moves_named_steps_first(svc):
    async def _go():
        plan = await svc.create(
            owner_id="u1", session_id="s1",
            steps=[{"content": "a"}, {"content": "b"}, {"content": "c"}],
        )
        a, b, c = (s.id for s in plan.steps)
        plan = await svc.reorder(owner_id="u1", session_id="s1", step_ids=[c, a])
        assert [s.content for s in plan.steps] == ["c", "a", "b"]  # b kept, appended

    _run(_go())


def test_dependencies_are_preserved(svc):
    async def _go():
        plan = await svc.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}])
        first = plan.steps[0].id
        plan = await svc.add_step(
            owner_id="u1", session_id="s1", content="b", depends_on=[first]
        )
        assert plan.steps[1].depends_on == (first,)

    _run(_go())


def test_clear_empties_steps_but_keeps_plan_record(svc):
    async def _go():
        plan = await svc.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}])
        plan_id = plan.id
        cleared = await svc.clear(owner_id="u1", session_id="s1")
        assert cleared.id == plan_id
        assert cleared.steps == ()

    _run(_go())


def test_archive_sets_archived_at(svc):
    async def _go():
        await svc.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}])
        archived = await svc.archive(owner_id="u1", session_id="s1")
        assert archived.archived_at is not None

    _run(_go())


def test_archive_missing_plan_raises_not_found(svc):
    async def _go():
        with pytest.raises(PlanNotFound):
            await svc.archive(owner_id="u1", session_id="never-created")

    _run(_go())


# ── Optimistic concurrency ───────────────────────────────────────────────

def test_stale_expected_version_raises_conflict_with_latest(svc):
    async def _go():
        plan = await svc.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}])
        stale_version = plan.version
        await svc.add_step(owner_id="u1", session_id="s1", content="b")  # bumps version

        with pytest.raises(PlanVersionConflict) as exc_info:
            await svc.add_step(
                owner_id="u1", session_id="s1", content="c",
                expected_version=stale_version,
            )
        conflict = exc_info.value
        assert conflict.expected_version == stale_version
        assert conflict.latest.version > stale_version
        assert [s.content for s in conflict.latest.steps] == ["a", "b"]

    _run(_go())


def test_correct_expected_version_succeeds(svc):
    async def _go():
        plan = await svc.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}])
        updated = await svc.add_step(
            owner_id="u1", session_id="s1", content="b",
            expected_version=plan.version,
        )
        assert [s.content for s in updated.steps] == ["a", "b"]

    _run(_go())


# ── Isolation ─────────────────────────────────────────────────────────────

def test_different_sessions_are_isolated(svc):
    async def _go():
        await svc.create(owner_id="u1", session_id="s1", steps=[{"content": "s1-step"}])
        await svc.create(owner_id="u1", session_id="s2", steps=[{"content": "s2-step"}])
        p1 = await svc.read(owner_id="u1", session_id="s1")
        p2 = await svc.read(owner_id="u1", session_id="s2")
        assert [s.content for s in p1.steps] == ["s1-step"]
        assert [s.content for s in p2.steps] == ["s2-step"]

    _run(_go())


def test_different_owners_same_session_id_are_isolated(svc):
    async def _go():
        await svc.create(owner_id="alice", session_id="shared", steps=[{"content": "alice-step"}])
        await svc.create(owner_id="bob", session_id="shared", steps=[{"content": "bob-step"}])
        alice_plan = await svc.read(owner_id="alice", session_id="shared")
        bob_plan = await svc.read(owner_id="bob", session_id="shared")
        assert [s.content for s in alice_plan.steps] == ["alice-step"]
        assert [s.content for s in bob_plan.steps] == ["bob-step"]

    _run(_go())


def test_concurrent_updates_to_same_plan_serialize_without_losing_writes(svc):
    async def _go():
        await svc.create(owner_id="u1", session_id="s1", steps=[])
        await asyncio.gather(
            *(svc.add_step(owner_id="u1", session_id="s1", content=f"step {i}") for i in range(10))
        )
        plan = await svc.read(owner_id="u1", session_id="s1")
        assert len(plan.steps) == 10
        assert len({s.id for s in plan.steps}) == 10  # every id unique, nothing clobbered

    _run(_go())


# ── Persistence / reload ─────────────────────────────────────────────────

def test_reload_from_disk_via_a_fresh_service_instance(tmp_path):
    root = str(tmp_path / "agent_plans")

    async def _write():
        svc1 = PlanService(root=root)
        await svc1.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}])

    async def _read():
        svc2 = PlanService(root=root)  # simulates a process restart
        return await svc2.read(owner_id="u1", session_id="s1")

    _run(_write())
    plan = _run(_read())
    assert plan is not None
    assert plan.steps[0].content == "a"


def test_persisted_file_is_valid_json_atomic_write(tmp_path):
    root = tmp_path / "agent_plans"
    svc = PlanService(root=str(root))
    _run(svc.create(owner_id="u1", session_id="s1", steps=[{"content": "a"}]))

    files = list(root.glob("*.json"))
    assert len(files) == 1
    assert not list(root.glob("*.tmp"))  # no leftover temp file
    with open(files[0]) as f:
        data = json.load(f)
    assert data["steps"][0]["content"] == "a"


# ── Markdown checklist round-trip (update_plan adapter) ──────────────────

def test_markdown_checklist_parses_status_and_content():
    steps = markdown_checklist_to_steps("- [x] done thing\n- [ ] pending thing")
    assert steps == [
        {"content": "done thing", "status": "completed"},
        {"content": "pending thing", "status": "pending"},
    ]


def test_markdown_checklist_ignores_non_checklist_lines():
    steps = markdown_checklist_to_steps("## Plan\n- [ ] a\n\nsome prose\n- [x] b")
    assert [s["content"] for s in steps] == ["a", "b"]


def test_markdown_round_trip_is_stable():
    original = "- [x] a\n- [ ] b\n- [ ] c"
    steps = markdown_checklist_to_steps(original)
    from src.agent.planning.service import PlanStep as _PlanStep

    plan_steps = [_PlanStep(id=f"s{i}", **s) for i, s in enumerate(steps)]
    regenerated = steps_to_markdown_checklist(plan_steps)
    assert regenerated == original
