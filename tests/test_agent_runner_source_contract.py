import ast
from pathlib import Path


def _tree(path: Path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path):
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom):
            yield node.module or ""
        elif isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)


def _legacy_stream_imports(path: Path):
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module != "src.agent_loop":
            continue
        if any(alias.name == "stream_agent_loop" for alias in node.names):
            yield node


def test_api_does_not_own_runtime_or_legacy_projection():
    root = Path(__file__).resolve().parents[1]
    path = root / "src" / "agent" / "api.py"
    imports = tuple(_imports(path))
    assert "runner" in imports
    assert "src.agent_loop" not in imports
    assert not any(name.startswith("src.agent.runtime_v") for name in imports)

    tree = _tree(path)
    defined_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assigned_names = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            list(node.targets) if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Name)
    }
    assert "_legacy_arguments" not in defined_names
    assert "_legacy_stream" not in assigned_names


def test_only_runner_is_allowed_to_import_legacy_stream_from_agent_package():
    root = Path(__file__).resolve().parents[1]
    agent_root = root / "src" / "agent"
    offenders = []
    for path in agent_root.rglob("*.py"):
        if path.name == "runner.py":
            continue
        if tuple(_legacy_stream_imports(path)):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
