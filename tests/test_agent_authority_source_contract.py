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


def test_chat_route_uses_stable_authority_api_not_runtime_v2_authority():
    root = Path(__file__).resolve().parents[1]
    route = root / "routes" / "chat_routes.py"
    imports = tuple(_imports(route))
    source = route.read_text(encoding="utf-8")

    assert "src.agent.api" in imports
    assert "src.agent.contracts" in imports
    assert "src.agent.runtime_v2.authority" not in imports
    assert "src.agent.runtime_v2.contracts" not in imports
    assert "prepare_execution_context_async" not in source
    assert "RunBudgets(" not in source
    assert "Capability.WORKSPACE_WRITE" not in source
    assert "AgentAuthorityRequest(" in source
    assert "prepare_authority(" in source
    assert "_prepared_authority.event_payload()" in source


def test_routes_do_not_construct_runtime_v2_agent_authority_directly():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in (root / "routes").rglob("*.py"):
        imports = tuple(_imports(path))
        if "src.agent.runtime_v2.authority" in imports:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
