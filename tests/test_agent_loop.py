"""Tests for agent_loop.py — _detect_admin_intent, _compute_final_metrics,
and _append_tool_results. Uses mock imports to avoid loading the full app stack."""

import sys
from unittest.mock import MagicMock

_MOCKED_IMPORTS = [
    'sqlalchemy', 'sqlalchemy.orm', 'sqlalchemy.ext', 'sqlalchemy.ext.declarative',
    'sqlalchemy.ext.hybrid', 'sqlalchemy.sql', 'sqlalchemy.sql.expression',
    'src.database',
    # Keep src.agent_tools real: Runtime V2 imports package submodules such as
    # src.agent_tools.subprocess_tools while src.agent_loop is imported.
    'core.models', 'core.database',
]
_INJECTED_IMPORT_STUBS = {}
_PREEXISTING_AGENT_LOOP = sys.modules.get("src.agent_loop")


def _drop_module_if_same(name, expected):
    if sys.modules.get(name) is expected:
        sys.modules.pop(name, None)
    parent_name, _, attr = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None and getattr(parent, "__dict__", {}).get(attr) is expected:
        delattr(parent, attr)


# Mock heavy dependencies before importing. Only clean up stubs this file
# created so pre-existing conftest/pytest modules keep their intended state.
for mod in _MOCKED_IMPORTS:
    if mod not in sys.modules:
        stub = MagicMock()
        sys.modules[mod] = stub
        _INJECTED_IMPORT_STUBS[mod] = stub

_IMPORTED_AGENT_LOOP = None
try:
    from src.agent_loop import (
        _detect_admin_intent,
        _classify_agent_request,
        _compute_final_metrics,
        _append_tool_results,
        _insert_before_latest_user,
        _MCP_KEYWORDS,
    )
    _IMPORTED_AGENT_LOOP = sys.modules.get("src.agent_loop")
finally:
    if _PREEXISTING_AGENT_LOOP is None and _IMPORTED_AGENT_LOOP is not None:
        _drop_module_if_same("src.agent_loop", _IMPORTED_AGENT_LOOP)
    for _mod, _stub in _INJECTED_IMPORT_STUBS.items():
        _drop_module_if_same(_mod, _stub)


def test_import_stubs_do_not_leak_into_later_tests():
    leaked = [
        mod for mod, stub in _INJECTED_IMPORT_STUBS.items()
        if sys.modules.get(mod) is stub
    ]
    assert leaked == []
    if _PREEXISTING_AGENT_LOOP is None:
        assert sys.modules.get("src.agent_loop") is not _IMPORTED_AGENT_LOOP


def test_mcp_keyword_gate_matches_literal_mcp_requests():
    assert "mcp" in _MCP_KEYWORDS


def test_polish_internet_search_request_classifies_as_web():
    intent = _classify_agent_request(
        [],
        "Wyszukaj w internecie i podaj temperaturę w Lubartowie dzisiaj",
    )

    assert intent["low_signal"] is False
    assert "web" in intent["domains"]


def test_insert_before_latest_user_places_context_before_last_user_turn():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "latest"},
    ]
    context = {"role": "system", "content": "context"}

    out = _insert_before_latest_user(messages, context)

    assert out == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        context,
        {"role": "user", "content": "latest"},
    ]
    assert messages == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "latest"},
    ]


def test_insert_before_latest_user_appends_when_no_user_message_exists():
    messages = [{"role": "assistant", "content": "reply"}]
    context = {"role": "system", "content": "context"}

    assert _insert_before_latest_user(messages, context) == [messages[0], context]


# ---------------------------------------------------------------------------
# _detect_admin_intent
# ---------------------------------------------------------------------------

class TestDetectAdminIntent:
    """Test admin-intent detection from the last user message."""

    def _msgs(self, text: str):
        """Helper: wrap text in a minimal messages list."""
        return [{"role": "user", "content": text}]

    # --- Should detect admin intent ---

    def test_add_endpoint(self):
        messages = self._msgs("Add endpoint http://localhost:11434/v1")
        assert _detect_admin_intent(messages)

    def test_configure_endpoint(self):
        messages = self._msgs("Configure my endpoint")
        assert _detect_admin_intent(messages)

    def test_manage_endpoint(self):
        messages = self._msgs("Manage endpoint settings")
        assert _detect_admin_intent(messages)

    def test_start_server(self):
        messages = self._msgs("Start server llama.cpp")
        assert _detect_admin_intent(messages)

    def test_stop_server(self):
        messages = self._msgs("Stop server llama.cpp")
        assert _detect_admin_intent(messages)

    def test_restart_server(self):
        messages = self._msgs("Restart server llama.cpp")
        assert _detect_admin_intent(messages)

    def test_delete_endpoint(self):
        messages = self._msgs("Delete endpoint 5")
        assert _detect_admin_intent(messages)

    def test_remove_model(self):
        messages = self._msgs("Remove model foo")
        assert _detect_admin_intent(messages)

    def test_cookbook_intent(self):
        messages = self._msgs("Install cookbook model")
        assert _detect_admin_intent(messages)

    # --- Should NOT detect admin intent ---

    def test_normal_question(self):
        messages = self._msgs("What is the capital of France?")
        assert not _detect_admin_intent(messages)

    def test_endpoint_word_in_question(self):
        messages = self._msgs("What is an API endpoint?")
        assert not _detect_admin_intent(messages)

    def test_empty_messages(self):
        assert not _detect_admin_intent([])

    def test_no_user_message(self):
        assert not _detect_admin_intent([{"role": "assistant", "content": "hi"}])


# ---------------------------------------------------------------------------
# _compute_final_metrics
# ---------------------------------------------------------------------------

class TestComputeFinalMetrics:
    """Test final metrics calculation."""

    def test_zero_values(self):
        result = _compute_final_metrics(0, 0, 0, 0)
        assert result["rounds"] == 0
        assert result["tool_calls"] == 0

    def test_basic_values(self):
        result = _compute_final_metrics(3, 5, 100, 200)
        assert result["rounds"] == 3
        assert result["tool_calls"] == 5
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 200
        assert result["total_tokens"] == 300


# ---------------------------------------------------------------------------
# _append_tool_results
# ---------------------------------------------------------------------------

class TestAppendToolResults:
    """Test tool result message construction."""

    def test_appends_tool_result(self):
        messages = []
        _append_tool_results(messages, [{"name": "test", "result": "ok"}])
        assert len(messages) == 1
        assert messages[0]["role"] == "tool"

    def test_multiple_results(self):
        messages = []
        results = [
            {"name": "one", "result": "1"},
            {"name": "two", "result": "2"},
        ]
        _append_tool_results(messages, results)
        assert len(messages) == 2
