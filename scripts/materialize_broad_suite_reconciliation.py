#!/usr/bin/env python3
"""Development-only materializer for Agent Lab broad-suite reconciliation."""

from __future__ import annotations

import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_exact(path: Path, old: str, new: str, *, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    actual = text.count(old)
    if actual != count:
        raise SystemExit(f"{path}: expected {count} matches, found {actual}: {old!r}")
    path.write_text(text.replace(old, new, count), encoding="utf-8")
    if path.suffix == ".py":
        py_compile.compile(str(path), doraise=True)


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    py_compile.compile(str(path), doraise=True)


def rewrite_api_monkeypatches() -> None:
    replace_exact(
        ROOT / "tests/test_scheduler_prompt_cache_time.py",
        'monkeypatch.setattr("src.agent_loop.stream_agent_loop", _stub_stream)',
        'monkeypatch.setattr("src.agent.api.stream_agent_loop", _stub_stream)',
    )
    replace_exact(
        ROOT / "tests/test_teacher_eval_tier2.py",
        'monkeypatch.setattr("src.agent_loop.stream_agent_loop", fake_stream_agent_loop)',
        'monkeypatch.setattr("src.agent.api.stream_agent_loop", fake_stream_agent_loop)',
    )


def rewrite_fallback_contract() -> None:
    path = ROOT / "tests/test_llm_core_fallback.py"
    text = path.read_text(encoding="utf-8")
    old = '[("u1", "primary", {}), ("u2", "backup", {})], [{"role": "user", "content": "hi"}]'
    new = '[("https://provider.test/v1", "primary", {}), ("https://provider.test/v1", "backup", {})], [{"role": "user", "content": "hi"}]'
    if text.count(old) != 1:
        raise SystemExit("fallback helper candidate contract changed unexpectedly")
    text = text.replace(old, new, 1)
    old_cands = 'cands = [("u1", "m1", {}), ("u1", "m1", {}), ("u2", "m2", {})]'
    new_cands = 'cands = [("u1", "m1", {}), ("u1", "m1", {}), ("u1", "m2", {})]'
    if text.count(old_cands) != 1:
        raise SystemExit("duplicate fallback candidate fixture changed unexpectedly")
    text = text.replace(old_cands, new_cands, 1)
    old_assert = 'assert calls == [("u1", "m1"), ("u2", "m2")], f"duplicate route re-attempted: {calls}"'
    new_assert = 'assert calls == [("u1", "m1"), ("u1", "m2")], f"duplicate route re-attempted: {calls}"'
    if text.count(old_assert) != 1:
        raise SystemExit("duplicate fallback assertion changed unexpectedly")
    text = text.replace(old_assert, new_assert, 1)
    path.write_text(text, encoding="utf-8")
    py_compile.compile(str(path), doraise=True)


def rewrite_browser_mcp_tests() -> None:
    write(
        ROOT / "tests/test_builtin_mcp_npx_cache.py",
        '''import importlib.util\nfrom pathlib import Path\nimport sys\nimport types\n\n\nROOT = Path(__file__).resolve().parent.parent\n\n\ndef _load_builtin_mcp(monkeypatch):\n    core = types.ModuleType("core")\n    core.__path__ = []\n    platform_compat = types.ModuleType("core.platform_compat")\n    platform_compat.IS_WINDOWS = False\n    monkeypatch.setitem(sys.modules, "core", core)\n    monkeypatch.setitem(sys.modules, "core.platform_compat", platform_compat)\n\n    spec = importlib.util.spec_from_file_location(\n        "builtin_mcp_under_test",\n        ROOT / "src" / "builtin_mcp.py",\n    )\n    module = importlib.util.module_from_spec(spec)\n    assert spec.loader is not None\n    spec.loader.exec_module(module)\n    return module\n\n\ndef _install_local_binary(tmp_path: Path):\n    node_modules = tmp_path / "node_modules"\n    target = node_modules / "@playwright" / "mcp" / "cli.js"\n    target.parent.mkdir(parents=True)\n    target.write_text("#!/usr/bin/env node\\n", encoding="utf-8")\n    shim = node_modules / ".bin" / "playwright-mcp"\n    shim.parent.mkdir(parents=True)\n    shim.symlink_to(target)\n    return shim, target\n\n\ndef test_local_node_binary_resolves_lockfile_installed_shim(monkeypatch, tmp_path):\n    builtin_mcp = _load_builtin_mcp(monkeypatch)\n    shim, _target = _install_local_binary(tmp_path)\n\n    assert builtin_mcp._find_local_node_binary("playwright-mcp", str(tmp_path)) == str(shim)\n\n\ndef test_local_node_binary_rejects_target_escaping_node_modules(monkeypatch, tmp_path):\n    builtin_mcp = _load_builtin_mcp(monkeypatch)\n    outside = tmp_path / "outside.js"\n    outside.write_text("x", encoding="utf-8")\n    shim = tmp_path / "node_modules" / ".bin" / "playwright-mcp"\n    shim.parent.mkdir(parents=True)\n    shim.symlink_to(outside)\n\n    assert builtin_mcp._find_local_node_binary("playwright-mcp", str(tmp_path)) is None\n\n\ndef test_browser_mcp_args_use_existing_configured_browser(monkeypatch, tmp_path):\n    browser = tmp_path / "chromium"\n    browser.write_text("browser", encoding="utf-8")\n    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", str(browser))\n    builtin_mcp = _load_builtin_mcp(monkeypatch)\n\n    args = builtin_mcp._browser_mcp_args(["--headless"])\n\n    assert args[args.index("--executable-path") + 1] == str(browser.resolve())\n    assert "--isolated" in args\n    assert "--no-sandbox" not in args\n\n\ndef test_browser_mcp_args_can_use_persistent_profile(monkeypatch, tmp_path):\n    browser = tmp_path / "chromium"\n    browser.write_text("browser", encoding="utf-8")\n    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", str(browser))\n    monkeypatch.setenv("ODYSSEUS_BROWSER_ISOLATED", "0")\n    builtin_mcp = _load_builtin_mcp(monkeypatch)\n\n    args = builtin_mcp._browser_mcp_args(["--headless", "--user-data-dir", "/tmp/profile"])\n\n    assert "--user-data-dir" in args\n    assert "--isolated" not in args\n\n\ndef test_browser_mcp_no_sandbox_requires_explicit_opt_in(monkeypatch, tmp_path):\n    browser = tmp_path / "chromium"\n    browser.write_text("browser", encoding="utf-8")\n    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", str(browser))\n    monkeypatch.setenv("ODYSSEUS_BROWSER_NO_SANDBOX", "1")\n    builtin_mcp = _load_builtin_mcp(monkeypatch)\n\n    assert "--no-sandbox" in builtin_mcp._browser_mcp_args(["--headless"])\n''',
    )

    write(
        ROOT / "tests/test_builtin_mcp_bg_tasks.py",
        '''"""Built-in MCP background tasks retain strong references until terminal."""\n\nimport asyncio\nimport importlib.util\nimport sys\nimport types\nfrom pathlib import Path\n\nimport pytest\n\n\nROOT = Path(__file__).resolve().parent.parent\n\n\ndef _load_builtin_mcp(monkeypatch):\n    core = types.ModuleType("core")\n    core.__path__ = []\n    platform_compat = types.ModuleType("core.platform_compat")\n    platform_compat.IS_WINDOWS = False\n    monkeypatch.setitem(sys.modules, "core", core)\n    monkeypatch.setitem(sys.modules, "core.platform_compat", platform_compat)\n\n    spec = importlib.util.spec_from_file_location(\n        "builtin_mcp_under_test",\n        ROOT / "src" / "builtin_mcp.py",\n    )\n    module = importlib.util.module_from_spec(spec)\n    assert spec.loader is not None\n    spec.loader.exec_module(module)\n    return module\n\n\nasync def test_spawn_bg_holds_strong_ref_until_task_finishes(monkeypatch):\n    builtin_mcp = _load_builtin_mcp(monkeypatch)\n    started = asyncio.Event()\n    release = asyncio.Event()\n\n    async def work():\n        started.set()\n        await release.wait()\n\n    task = builtin_mcp._spawn_bg(work())\n    await started.wait()\n    assert task in builtin_mcp._BG_TASKS\n\n    release.set()\n    await task\n    await asyncio.sleep(0)\n    assert task not in builtin_mcp._BG_TASKS\n\n\nasync def test_spawn_bg_discards_cancelled_task(monkeypatch):\n    builtin_mcp = _load_builtin_mcp(monkeypatch)\n\n    task = builtin_mcp._spawn_bg(asyncio.sleep(3600))\n    assert task in builtin_mcp._BG_TASKS\n    task.cancel()\n    with pytest.raises(asyncio.CancelledError):\n        await task\n    await asyncio.sleep(0)\n    assert task not in builtin_mcp._BG_TASKS\n''',
    )

    write(
        ROOT / "tests/test_mcp_manager.py",
        '''import asyncio\nfrom unittest.mock import patch\n\nfrom src.mcp_manager import _format_mcp_connection_error, McpManager\n\n\ndef test_playwright_mcp_connection_error_includes_lockfile_install_hint():\n    msg = _format_mcp_connection_error(\n        "Built-in: Browser",\n        "/app/node_modules/.bin/playwright-mcp",\n        ["--headless"],\n        RuntimeError("package not found"),\n    )\n\n    assert "package not found" in msg\n    assert "lockfile-installed Browser MCP runtime could not start" in msg\n    assert "npm ci --omit=dev --ignore-scripts" in msg\n    assert "node_modules/.bin/playwright-mcp" in msg\n    assert "restart Odysseus" in msg\n    assert "npx -y" not in msg\n\n\ndef test_generic_mcp_connection_error_preserves_original_error():\n    msg = _format_mcp_connection_error(\n        "Custom MCP",\n        "python",\n        ["server.py"],\n        RuntimeError("boom"),\n    )\n\n    assert msg == "boom"\n\n\ndef test_http_transport_routes_to_start_http_connect():\n    mgr = McpManager()\n\n    async def fake_start(server_id, name, url):\n        return "ROUTED"\n\n    with patch.object(McpManager, "_start_http_connect", side_effect=fake_start) as m:\n        result = asyncio.run(mgr.connect_server("id1", "n", "http", url="https://x/mcp"))\n    assert result == "ROUTED"\n    m.assert_called_once()\n''',
    )


def rewrite_effect_resolution_diagnostics() -> None:
    path = ROOT / "src/agent/runtime_v3/executor_bridge.py"
    text = path.read_text(encoding="utf-8")
    anchor = "Implementation = Callable[..., Awaitable[ToolResult]]\n\n\n"
    if text.count(anchor) != 1:
        raise SystemExit("executor bridge implementation anchor changed")
    text = text.replace(
        anchor,
        anchor + "class EffectResolutionError(ValueError):\n    \"\"\"Effect targeting failed before a durable lease could be created.\"\"\"\n\n\n",
        1,
    )
    old = "    effects = tuple(definition.resolve_effects(arguments, context))\n"
    new = (
        "    try:\n"
        "        effects = tuple(definition.resolve_effects(arguments, context))\n"
        "    except ValueError as exc:\n"
        "        raise EffectResolutionError(str(exc)) from exc\n"
    )
    if text.count(old) != 1:
        raise SystemExit("effect resolver call changed unexpectedly")
    text = text.replace(old, new, 1)
    generic = "    except (OSError, RuntimeError, TypeError, ValueError) as exc:\n"
    if text.count(generic) != 1:
        raise SystemExit("preflight catch changed unexpectedly")
    specific = (
        "    except EffectResolutionError as exc:\n"
        "        _invalidate_approval_after_preflight_failure(approval_id)\n"
        "        return _bridge_error(\n"
        "            call,\n"
        "            code=\"effect_resolution_error\",\n"
        "            message=str(exc),\n"
        "            status=(ToolResultStatus.DENIED if approval_id else ToolResultStatus.INCOMPLETE),\n"
        "        )\n"
    )
    text = text.replace(generic, specific + generic, 1)
    path.write_text(text, encoding="utf-8")
    py_compile.compile(str(path), doraise=True)

    test_path = ROOT / "tests/runtime_v3/test_executor_bridge.py"
    test = test_path.read_text(encoding="utf-8")
    old_import = "from src.agent.runtime_v3.executor_bridge import execute_with_durable_effects\n"
    new_import = (
        "from src.agent.runtime_v3.executor_bridge import (\n"
        "    EffectResolutionError,\n"
        "    execute_with_durable_effects,\n"
        ")\n"
    )
    if test.count(old_import) != 1:
        raise SystemExit("executor bridge test import changed unexpectedly")
    test = test.replace(old_import, new_import, 1)
    anchor = "    async def test_approved_preflight_failure_denies_before_handler(self):\n"
    if test.count(anchor) != 1:
        raise SystemExit("executor bridge insertion anchor changed unexpectedly")
    case = '''    async def test_effect_resolution_failure_preserves_specific_error(self):\n        calls = 0\n\n        async def implementation(*args, **kwargs):\n            nonlocal calls\n            calls += 1\n            raise AssertionError("handler must not execute")\n\n        with patch(\n            "src.agent.runtime_v3.executor_bridge._preflight_effects",\n            side_effect=EffectResolutionError("outside the execution root"),\n        ):\n            result = await execute_with_durable_effects(\n                self.call, self.context, implementation=implementation\n            )\n\n        self.assertEqual(calls, 0)\n        self.assertEqual(result.status, ToolResultStatus.INCOMPLETE)\n        self.assertEqual(result.error.code, "effect_resolution_error")\n        self.assertIn("outside the execution root", result.error.message)\n\n'''
    test = test.replace(anchor, case + anchor, 1)
    test_path.write_text(test, encoding="utf-8")
    py_compile.compile(str(test_path), doraise=True)


def main() -> None:
    rewrite_api_monkeypatches()
    rewrite_fallback_contract()
    rewrite_browser_mcp_tests()
    rewrite_effect_resolution_diagnostics()


if __name__ == "__main__":
    main()
