import ast
from pathlib import Path


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            yield node.module or ""
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def test_api_does_not_own_runtime_or_legacy_projection():
    root = Path(__file__).resolve().parents[1]
    path = root / "src" / "agent" / "api.py"
    imports = tuple(_imports(path))
    assert "runner" in imports
    assert "src.agent_loop" not in imports
    assert not any(name.startswith("src.agent.runtime_v") for name in imports)
    source = path.read_text(encoding="utf-8")
    assert "_legacy_arguments" not in source
    assert "_legacy_stream" not in source


def test_only_runner_is_allowed_to_reference_legacy_stream_from_agent_package():
    root = Path(__file__).resolve().parents[1]
    agent_root = root / "src" / "agent"
    offenders = []
    for path in agent_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "stream_agent_loop" not in source:
            continue
        if path.name == "runner.py":
            continue
        if path.name == "api.py":
            # Frozen public compatibility function name, not a legacy import.
            continue
        offenders.append(str(path.relative_to(root)))
    assert offenders == []
