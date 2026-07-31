"""Coverage for per-run repeated-read protection."""

from src.agent.execution.observation_ledger import ObservationLedger


def test_first_observation_returns_no_warning():
    ledger = ObservationLedger()
    assert ledger.note(tool="read_file", key="/a.py") is None


def test_exact_repeat_returns_a_warning_referencing_prior():
    ledger = ObservationLedger()
    ledger.note(tool="read_file", key="/a.py")
    warning = ledger.note(tool="read_file", key="/a.py")
    assert warning is not None
    assert "/a.py" in warning
    assert "read_file" in warning


def test_repeat_with_different_range_is_not_flagged():
    ledger = ObservationLedger()
    ledger.note(tool="read_file", key="/a.py", range_=(1, 100))
    warning = ledger.note(tool="read_file", key="/a.py", range_=(101, 200))
    assert warning is None


def test_same_range_different_tool_is_not_flagged():
    ledger = ObservationLedger()
    ledger.note(tool="read_file", key="/a.py")
    warning = ledger.note(tool="grep", key="/a.py")
    assert warning is None


def test_changed_identity_is_treated_as_a_genuine_reread_not_a_repeat():
    ledger = ObservationLedger()
    ledger.note(tool="read_file", key="/a.py", identity=("mtime1", 100))
    # File changed on disk between reads -> different identity -> no warning.
    warning = ledger.note(tool="read_file", key="/a.py", identity=("mtime2", 105))
    assert warning is None


def test_unchanged_identity_is_flagged():
    ledger = ObservationLedger()
    ledger.note(tool="read_file", key="/a.py", identity=("mtime1", 100))
    warning = ledger.note(tool="read_file", key="/a.py", identity=("mtime1", 100))
    assert warning is not None


def test_third_repeat_still_flags_and_updates_timestamp():
    ledger = ObservationLedger()
    ledger.note(tool="grep", key="TODO")
    ledger.note(tool="grep", key="TODO")
    warning = ledger.note(tool="grep", key="TODO")
    assert warning is not None


def test_legitimate_rereads_are_never_blocked_only_warned():
    # note() always records and returns a note (or None) — it never raises
    # or refuses to record the observation.
    ledger = ObservationLedger()
    for _ in range(5):
        result = ledger.note(tool="ls", key="/dir")
        assert result is None or isinstance(result, str)
    assert len(ledger) == 1  # one distinct (tool, key, range) triple


def test_concurrent_runs_do_not_share_ledgers():
    # Each run must construct its own instance — verifies there is no
    # ambient/global/class-level state a second instance could leak from.
    run_a = ObservationLedger()
    run_b = ObservationLedger()
    run_a.note(tool="read_file", key="/shared.py")
    # A brand-new ledger for a different run has no memory of run_a's read.
    warning = run_b.note(tool="read_file", key="/shared.py")
    assert warning is None
    assert len(run_a) == 1
    assert len(run_b) == 1


def test_live_result_helper_flags_same_request_and_same_content():
    ledger = ObservationLedger()
    kwargs = {
        "tool": "read_file",
        "content": '{"path":"src/app.py","offset":1,"limit":20}',
        "result": {"output": "same bytes", "exit_code": 0},
    }
    assert ledger.note_tool_result(**kwargs) is None
    assert ledger.note_tool_result(**kwargs) is not None


def test_live_result_helper_does_not_flag_changed_content():
    ledger = ObservationLedger()
    common = {
        "tool": "grep",
        "content": '{"pattern":"TODO","path":"src"}',
    }
    assert ledger.note_tool_result(
        **common, result={"output": "src/a.py:1:TODO", "exit_code": 0}
    ) is None
    assert ledger.note_tool_result(
        **common, result={"output": "src/b.py:2:TODO", "exit_code": 0}
    ) is None
