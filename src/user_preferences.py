"""Persistence service for per-user preferences.

HTTP routes and the agent runtime depend on this application-level service;
the service never imports route modules.
"""

import json
import os
from typing import Optional

from src.constants import USER_PREFS_FILE


def load_preferences(*, path: str = USER_PREFS_FILE) -> dict:
    """Load the complete preferences document."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_preferences(prefs: dict, *, path: str = USER_PREFS_FILE) -> None:
    """Atomically persist the complete preferences document."""

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    temporary = f"{path}.tmp.{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(prefs, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_user_preferences(
    user: Optional[str] = None,
    *,
    path: str = USER_PREFS_FILE,
) -> dict:
    """Load preferences for one owner, preserving legacy flat documents."""

    all_preferences = load_preferences(path=path)
    if "_users" in all_preferences:
        if user is None:
            users = all_preferences["_users"]
            return dict(next(iter(users.values()), {}))
        return dict(all_preferences["_users"].get(user, {}))
    return dict(all_preferences)


def save_user_preferences(
    user: Optional[str],
    prefs: dict,
    *,
    path: str = USER_PREFS_FILE,
) -> None:
    """Save one owner's preferences without clobbering other owners."""

    all_preferences = load_preferences(path=path)
    if user is None:
        if "_users" in all_preferences:
            users = all_preferences["_users"]
            first_key = next(iter(users), None)
            if first_key is not None:
                users[first_key] = prefs
                save_preferences(all_preferences, path=path)
                return
        save_preferences(prefs, path=path)
        return
    if "_users" not in all_preferences:
        all_preferences = {"_users": {}}
    all_preferences["_users"][user] = prefs
    save_preferences(all_preferences, path=path)
