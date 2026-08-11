import ast
from pathlib import Path


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield node.module or ""
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def test_runner_owns_lifecycle_service_not_runtime_v3_wrapper():
    root = Path(__file__).resolve().parents[1]
    runner = root / "src" / "agent" / "runner.py"
    imports = tuple(_imports(runner))

    assert "src.agent.runtime_v3.orchestrator" not in imports
    assert "src.agent.runtime_v3.lifecycle" not in imports  # relative import
    assert "runtime_v3.lifecycle" in imports
    assert "runtime_v3.orchestrator" not in imports


def test_runtime_v3_orchestrator_is_only_compatibility_facade():
    root = Path(__file__).resolve().parents[1]
    orchestrator = root / "src" / "agent" / "runtime_v3" / "orchestrator.py"
    imports = tuple(_imports(orchestrator))
    source = orchestrator.read_text(encoding="utf-8")

    assert "lifecycle" in imports
    assert "ledger" not in imports
    assert "request_identity" not in imports
    assert "DurableRunLifecycle()" in source
    assert "ledger.transition(" not in source
    assert "json.loads(" not in source


def test_terminal_state_source_is_typed_first_with_explicit_legacy_fallback():
    root = Path(__file__).resolve().parents[1]
    lifecycle = root / "src" / "agent" / "runtime_v3" / "lifecycle.py"
    source = lifecycle.read_text(encoding="utf-8")

    assert "observe_agent_events(self.observe_typed_event)" in source
    assert "observe_runtime_events(self.observe_runtime_event)" in source
    assert "terminal_from_agent_event" in source
    assert "terminal_from_runtime_event" in source
    assert "terminal_from_legacy_wire" in source
    assert "typed = self._typed_terminal_for_wire(wire)" in source
    assert "if typed is not None:" in source
    assert "return self._legacy_terminal_parser(wire)" in source


def test_both_typed_sse_encoders_expose_task_local_observation():
    root = Path(__file__).resolve().parents[1]
    agent_events = (root / "src" / "agent" / "events.py").read_text(encoding="utf-8")
    runtime_events = (
        root / "src" / "agent" / "runtime_v2" / "events.py"
    ).read_text(encoding="utf-8")

    assert "observe_agent_events" in agent_events
    assert "_notify_observers(event, wire)" in agent_events
    assert "observe_runtime_events" in runtime_events
    assert "_notify_runtime_event_observers(event, wire)" in runtime_events
