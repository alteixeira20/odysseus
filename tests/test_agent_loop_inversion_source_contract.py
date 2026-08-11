import ast
from pathlib import Path


def _tree(path: Path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(path: Path, name: str):
    matches = [
        node
        for node in _tree(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(matches) == 1, (path, name, len(matches))
    return matches[0]


def test_agent_loop_has_one_internal_kernel_and_one_public_facade():
    root = Path(__file__).resolve().parents[1]
    path = root / "src" / "agent_loop.py"

    kernel = _function(path, "_legacy_stream_agent_kernel")
    facade = _function(path, "stream_agent_loop")

    assert isinstance(kernel, ast.AsyncFunctionDef)
    assert isinstance(facade, ast.AsyncFunctionDef)
    assert kernel.lineno < facade.lineno


def test_public_facade_lazy_imports_only_stable_agent_api_for_execution():
    root = Path(__file__).resolve().parents[1]
    facade = _function(root / "src" / "agent_loop.py", "stream_agent_loop")

    imports = [node for node in ast.walk(facade) if isinstance(node, ast.ImportFrom)]
    assert len(imports) == 1
    assert imports[0].module == "src.agent.api"
    assert [(alias.name, alias.asname) for alias in imports[0].names] == [
        ("stream_agent_loop", "canonical_stream_agent_loop")
    ]

    called_names = {
        node.func.id
        for node in ast.walk(facade)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called_names == {"canonical_stream_agent_loop"}


def test_runner_backend_reaches_internal_kernel_not_public_facade():
    root = Path(__file__).resolve().parents[1]
    runner = (root / "src" / "agent" / "runner.py").read_text(encoding="utf-8")

    assert "from src.agent_loop import _legacy_stream_agent_kernel" in runner
    assert "return _legacy_stream_agent_kernel(**self.arguments(request))" in runner
    assert "from src.agent_loop import stream_agent_loop" not in runner
