"""Application-service boundary for agent-visible user preferences."""

import routes.prefs_routes as prefs_routes
from src.user_preferences import (
    load_user_preferences,
    save_user_preferences,
)


def test_route_compatibility_wrappers_share_service_semantics(monkeypatch, tmp_path):
    prefs_file = tmp_path / "user-prefs.json"
    monkeypatch.setattr(prefs_routes, "PREFS_FILE", str(prefs_file))

    prefs_routes._save_for_user("alice", {"skills_enabled": False})

    assert prefs_routes._load_for_user("alice") == {"skills_enabled": False}
    assert load_user_preferences("alice", path=str(prefs_file)) == {
        "skills_enabled": False,
    }


def test_single_user_write_preserves_existing_multi_user_store(tmp_path):
    prefs_file = tmp_path / "user-prefs.json"
    save_user_preferences("alice", {"theme": "light"}, path=str(prefs_file))
    save_user_preferences("bob", {"theme": "dark"}, path=str(prefs_file))

    current = load_user_preferences(None, path=str(prefs_file))
    current["skills_enabled"] = False
    save_user_preferences(None, current, path=str(prefs_file))

    assert load_user_preferences("alice", path=str(prefs_file)) == {
        "theme": "light",
        "skills_enabled": False,
    }
    assert load_user_preferences("bob", path=str(prefs_file)) == {
        "theme": "dark",
    }
