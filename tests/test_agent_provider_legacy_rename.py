"""Regression coverage for preserving legacy CLI-agent identity on rename."""

import asyncio
import contextlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def token_routes_mod(monkeypatch):
    mp_stub = types.ModuleType("python_multipart")
    mp_stub.__version__ = "0.0.13"
    monkeypatch.setitem(sys.modules, "python_multipart", mp_stub)

    class _DBStub(types.ModuleType):
        def __getattr__(self, name):
            return MagicMock()

    @contextlib.contextmanager
    def _noop_db_session():
        yield MagicMock()

    db_stub = _DBStub("core.database")
    db_stub.get_db_session = _noop_db_session
    db_stub.ApiToken = MagicMock()
    monkeypatch.setitem(sys.modules, "core.database", db_stub)
    monkeypatch.delitem(sys.modules, "routes.api_token_routes", raising=False)

    import routes.api_token_routes as mod

    return mod


def _handler(mod, method: str, path: str):
    router = mod.setup_api_token_routes()
    for route in router.routes:
        if getattr(route, "path", "") == path and method in (getattr(route, "methods", None) or set()):
            return route.endpoint
    raise AssertionError(f"missing {method} {path}")


@contextlib.contextmanager
def _db_ctx(session):
    yield session


def test_renaming_legacy_chat_only_agent_persists_provider(monkeypatch, token_routes_mod):
    """A legacy name-derived provider must survive a user-friendly rename."""
    mod = token_routes_mod

    token = SimpleNamespace(
        id="legacy01",
        owner="alice",
        name="Codex Agent Main",
        token_hash="hash",
        token_prefix="ody_old1",
        scopes="chat",
        agent_provider=None,
        is_active=True,
        last_used_at=None,
        created_at=None,
    )

    query = MagicMock()
    query.filter.return_value.first.return_value = token
    session = MagicMock()
    session.query.return_value = query

    monkeypatch.setattr(mod, "get_db_session", lambda: _db_ctx(session))
    monkeypatch.setattr(mod, "require_admin", lambda request: None)
    monkeypatch.setattr(mod, "get_current_user", lambda request: "alice")

    request = SimpleNamespace(
        state=SimpleNamespace(current_user="alice"),
        app=SimpleNamespace(state=SimpleNamespace()),
    )

    async def _json():
        return {"name": "Main workstation"}

    request.json = _json

    update = _handler(mod, "PATCH", "/api/tokens/{token_id}")
    response = asyncio.run(update(request=request, token_id=token.id))

    assert token.name == "Main workstation"
    assert token.agent_provider == "codex"
    assert response["agent_provider"] == "codex"
    assert response["scopes"] == ["chat"]
    session.add.assert_called_once_with(token)
