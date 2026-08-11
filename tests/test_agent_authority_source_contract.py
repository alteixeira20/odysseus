import ast
from pathlib import Path


def _tree(path: Path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path):
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom):
            yield node.module or ""
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def _imported_names(path: Path, module: str):
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            for alias in node.names:
                yield alias.name


def test_chat_route_uses_stable_api_for_agent_run_authority_preparation():
    root = Path(__file__).resolve().parents[1]
    route = root / "routes" / "chat_routes.py"
    imports = tuple(_imports(route))
    source = route.read_text(encoding="utf-8")

    assert "src.agent.api" in imports
    assert "src.agent.contracts" in imports
    assert "prepare_execution_context_async" not in tuple(
        _imported_names(route, "src.agent.runtime_v2.authority")
    )
    assert "prepare_execution_context_async" not in source
    assert "RunBudgets(" not in source
    assert "Capability.WORKSPACE_WRITE" not in source
    assert "AgentAuthorityRequest(" in source
    assert "prepare_authority(" in source
    assert "_prepared_authority.event_payload()" in source


def test_routes_do_not_prepare_agent_execution_context_directly():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in (root / "routes").rglob("*.py"):
        imported = tuple(_imported_names(path, "src.agent.runtime_v2.authority"))
        source = path.read_text(encoding="utf-8")
        if "prepare_execution_context_async" in imported or "prepare_execution_context_async(" in source:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_host_authorization_endpoint_remains_separate_from_run_preparation():
    root = Path(__file__).resolve().parents[1]
    route = root / "routes" / "chat_routes.py"
    source = route.read_text(encoding="utf-8")

    # This PR moves execution-context construction only. The one-run host-token
    # issuer remains an explicit separate boundary until its own migration.
    assert '@router.post("/api/chat/host-authorize")' in source
    assert "HOST_AUTHORIZATIONS.issue(" in source
