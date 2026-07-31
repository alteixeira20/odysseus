"""User preferences API backed by the application preference service."""
import os  # compatibility: tests/callers patch prefs_routes.os.replace
from typing import Optional
from fastapi import APIRouter, Request
from src.auth_helpers import get_current_user
from src.constants import USER_PREFS_FILE
from src.user_preferences import (
    load_preferences,
    load_user_preferences,
    save_preferences,
    save_user_preferences,
)

PREFS_FILE = USER_PREFS_FILE


def _load():
    """Load the raw prefs file (internal use only)."""
    return load_preferences(path=PREFS_FILE)


def _save(prefs):
    save_preferences(prefs, path=PREFS_FILE)


def _load_for_user(user: Optional[str] = None) -> dict:
    """Load preferences for a specific user."""
    return load_user_preferences(user, path=PREFS_FILE)


def _save_for_user(user: Optional[str], prefs: dict):
    """Save preferences for a specific user."""
    save_user_preferences(user, prefs, path=PREFS_FILE)


def setup_prefs_routes():
    router = APIRouter(prefix="/api/prefs", tags=["preferences"])

    @router.get("")
    async def get_all_prefs(request: Request):
        user = get_current_user(request)
        return _load_for_user(user)

    @router.get("/{key}")
    async def get_pref(request: Request, key: str):
        user = get_current_user(request)
        prefs = _load_for_user(user)
        return {"key": key, "value": prefs.get(key)}

    @router.put("/{key}")
    async def set_pref(request: Request, key: str, body: dict):
        user = get_current_user(request)
        prefs = _load_for_user(user)
        prefs[key] = body.get("value")
        _save_for_user(user, prefs)
        return {"key": key, "value": prefs[key]}

    return router
