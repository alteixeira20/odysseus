from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import agent_lab_readiness as readiness  # noqa: E402


def test_quick_suite_pins_high_risk_agent_invariants():
    targets = set(readiness.QUICK_PYTEST_TARGETS)
    required = {
        "tests/runtime_v3/test_effect_bridge.py",
        "tests/runtime_v3/test_executor_bridge.py",
        "tests/runtime_v3/test_request_identity.py",
        "tests/test_agent_completion_contract.py",
        "tests/test_agent_unknown_tool_recovery.py",
        "tests/test_agent_truncation_continuation.py",
        "tests/test_agent_verifier_truthfulness.py",
        "tests/test_runtime_v2_execution_integrity.py",
        "tests/test_runtime_v2_dependability.py",
    }
    assert required <= targets


def test_full_suite_matches_gate_categories_and_expands_globs():
    targets = readiness.full_pytest_targets()
    assert targets[0] == "tests/runtime_v3"
    assert "tests/test_agent_runtime_contract.py" in targets
    assert "tests/test_agent_verifier_truthfulness.py" in targets
    assert "tests/test_agent_lab_readiness.py" in targets
    assert "tests/test_runtime_v2_execution_integrity.py" in targets
    assert "tests/test_process_sandbox.py" in targets
    assert "tests/test_tool_registry.py" in targets
    assert len(targets) == len(set(targets))


def test_python_command_keeps_blocking_pytest_safety_flags():
    command = readiness.python_command(full=False)
    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert "--maxfail=20" in command
    assert "--timeout=90" in command
    assert "--timeout-method=thread" in command
    assert command[-1] == "tests/test_runtime_v2_dependability.py"


def test_compile_targets_cover_runtime_entrypoints():
    targets = set(readiness.COMPILE_TARGETS)
    assert {
        "src/agent",
        "src/agent_loop.py",
        "src/agent_tools",
        "src/llm_core.py",
        "src/mcp_manager.py",
        "routes/agent_runtime_routes.py",
        "routes/chat_routes.py",
        "tests/runtime_v3",
    } <= targets


def test_javascript_syntax_checks_are_one_file_per_command():
    commands = readiness.javascript_commands()
    syntax_commands = commands[:-1]
    test_command = commands[-1]
    assert len(syntax_commands) == len(readiness.JS_SYNTAX_TARGETS)
    for command, target in zip(syntax_commands, readiness.JS_SYNTAX_TARGETS):
        assert command == ["node", "--check", target]
    assert test_command == ["node", "--test", *readiness.JS_TEST_TARGETS]


def test_dry_run_does_not_execute_subprocess(monkeypatch, capsys):
    called = False

    def forbidden_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("dry-run must not execute subprocesses")

    monkeypatch.setattr(readiness.subprocess, "run", forbidden_run)
    readiness._run(["python", "-m", "pytest", "example"], dry_run=True)
    assert called is False
    assert "$ python -m pytest example" in capsys.readouterr().out
