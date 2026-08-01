import asyncio
import json

import pytest

from src import agent_runs, bg_jobs


async def _blocked_producer():
    await asyncio.Event().wait()
    yield "data: never\n\n"


@pytest.mark.asyncio
async def test_detached_run_wall_clock_limit_becomes_resumable_incomplete(monkeypatch):
    session_id = "wall-limit-contract"
    agent_runs._RUNS.pop(session_id, None)
    monkeypatch.setattr(agent_runs, "_MAX_RUN_WALL_CLOCK_S", 0.03)
    monkeypatch.setattr(agent_runs, "_MAX_RUN_IDLE_S", 10.0)
    run = agent_runs.start(session_id, _blocked_producer(), owner="wall-owner-unique")
    chunks = [chunk async for chunk in agent_runs.subscribe(session_id)]

    terminal = next(
        json.loads(chunk[6:])
        for chunk in chunks
        if '"type": "run_state"' in chunk
    )
    assert terminal == {
        "type": "run_state",
        "state": "incomplete",
        "terminal": True,
        "reason": "run_wall_clock_exhausted",
        "resumable": True,
    }
    assert run.status == "incomplete"
    agent_runs._RUNS.pop(session_id, None)


@pytest.mark.asyncio
async def test_detached_run_idle_limit_is_independent_from_subscribers(monkeypatch):
    session_id = "idle-limit-contract"
    agent_runs._RUNS.pop(session_id, None)
    monkeypatch.setattr(agent_runs, "_MAX_RUN_WALL_CLOCK_S", 10.0)
    monkeypatch.setattr(agent_runs, "_MAX_RUN_IDLE_S", 0.03)
    run = agent_runs.start(session_id, _blocked_producer(), owner="idle-owner-unique")
    await asyncio.wait_for(run.task, timeout=1)
    assert run.terminal is not None
    assert run.terminal.reason == "run_idle_timeout"
    agent_runs._RUNS.pop(session_id, None)


@pytest.mark.asyncio
async def test_owner_and_server_concurrency_limits_fail_before_replacement(monkeypatch):
    monkeypatch.setattr(agent_runs, "_MAX_ACTIVE_RUNS", 2)
    monkeypatch.setattr(agent_runs, "_MAX_ACTIVE_RUNS_PER_OWNER", 1)
    for key in ("quota-one", "quota-two", "quota-three"):
        agent_runs._RUNS.pop(key, None)
    first = agent_runs.start("quota-one", _blocked_producer(), owner="alice")
    with pytest.raises(agent_runs.RunCapacityError, match="owner"):
        agent_runs.start("quota-two", _blocked_producer(), owner="alice")
    second = agent_runs.start("quota-two", _blocked_producer(), owner="bob")
    with pytest.raises(agent_runs.RunCapacityError, match="server"):
        agent_runs.start("quota-three", _blocked_producer(), owner="carol")

    assert agent_runs.stop("quota-one")
    assert agent_runs.stop("quota-two")
    await asyncio.gather(first.task, second.task, return_exceptions=True)
    for key in ("quota-one", "quota-two", "quota-three"):
        agent_runs._RUNS.pop(key, None)


@pytest.mark.asyncio
async def test_run_listing_is_owner_scoped_and_excludes_replay_content():
    session_id = "listing-contract"
    agent_runs._RUNS.pop(session_id, None)
    run = agent_runs.start(session_id, _blocked_producer(), owner="listing-owner-unique")
    records = agent_runs.list_runs("listing-owner-unique")
    assert [record["session_id"] for record in records] == [session_id]
    assert "buffer" not in records[0]
    assert agent_runs.list_runs("missing-owner-unique") == []
    assert agent_runs.stop(session_id)
    await asyncio.gather(run.task, return_exceptions=True)
    await asyncio.sleep(0)
    assert run.terminal is not None
    assert run.terminal.disposition.value == "cancelled"
    agent_runs._RUNS.pop(session_id, None)


def test_background_job_session_quota_blocks_before_process_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(bg_jobs, "MAX_RUNNING_JOBS_GLOBAL", 20)
    monkeypatch.setattr(bg_jobs, "MAX_RUNNING_JOBS_PER_OWNER", 20)
    monkeypatch.setattr(bg_jobs, "MAX_RUNNING_JOBS_PER_SESSION", 1)
    monkeypatch.setattr(
        bg_jobs,
        "refresh",
        lambda: {
            "existing": {
                "status": "running",
                "session_id": "quota-session",
                "owner": "alice",
            }
        },
    )
    with pytest.raises(RuntimeError, match="session background-job limit"):
        bg_jobs.launch(
            "printf never",
            session_id="quota-session",
            cwd=str(tmp_path),
            owner="alice",
            execution_mode="host",
        )
