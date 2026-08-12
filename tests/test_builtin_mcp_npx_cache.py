import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parent.parent


def _load_builtin_mcp(monkeypatch):
    core = types.ModuleType("core")
    core.__path__ = []
    platform_compat = types.ModuleType("core.platform_compat")
    platform_compat.IS_WINDOWS = False
    monkeypatch.setitem(sys.modules, "core", core)
    monkeypatch.setitem(sys.modules, "core.platform_compat", platform_compat)

    spec = importlib.util.spec_from_file_location(
        "builtin_mcp_under_test",
        ROOT / "src" / "builtin_mcp.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _install_local_binary(tmp_path: Path):
    node_modules = tmp_path / "node_modules"
    target = node_modules / "@playwright" / "mcp" / "cli.js"
    target.parent.mkdir(parents=True)
    target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    shim = node_modules / ".bin" / "playwright-mcp"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(target)
    return shim, target


def test_local_node_binary_resolves_lockfile_installed_shim(monkeypatch, tmp_path):
    builtin_mcp = _load_builtin_mcp(monkeypatch)
    shim, _target = _install_local_binary(tmp_path)

    assert builtin_mcp._find_local_node_binary("playwright-mcp", str(tmp_path)) == str(shim)


def test_local_node_binary_rejects_target_escaping_node_modules(monkeypatch, tmp_path):
    builtin_mcp = _load_builtin_mcp(monkeypatch)
    outside = tmp_path / "outside.js"
    outside.write_text("x", encoding="utf-8")
    shim = tmp_path / "node_modules" / ".bin" / "playwright-mcp"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(outside)

    assert builtin_mcp._find_local_node_binary("playwright-mcp", str(tmp_path)) is None


def test_browser_mcp_args_use_existing_configured_browser(monkeypatch, tmp_path):
    browser = tmp_path / "chromium"
    browser.write_text("browser", encoding="utf-8")
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", str(browser))
    builtin_mcp = _load_builtin_mcp(monkeypatch)

    args = builtin_mcp._browser_mcp_args(["--headless"])

    assert args[args.index("--executable-path") + 1] == str(browser.resolve())
    assert "--isolated" in args
    assert "--no-sandbox" not in args


def test_browser_mcp_args_can_use_persistent_profile(monkeypatch, tmp_path):
    browser = tmp_path / "chromium"
    browser.write_text("browser", encoding="utf-8")
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", str(browser))
    monkeypatch.setenv("ODYSSEUS_BROWSER_ISOLATED", "0")
    builtin_mcp = _load_builtin_mcp(monkeypatch)

    args = builtin_mcp._browser_mcp_args(["--headless", "--user-data-dir", "/tmp/profile"])

    assert "--user-data-dir" in args
    assert "--isolated" not in args


def test_browser_mcp_no_sandbox_requires_explicit_opt_in(monkeypatch, tmp_path):
    browser = tmp_path / "chromium"
    browser.write_text("browser", encoding="utf-8")
    monkeypatch.setenv("ODYSSEUS_BROWSER_EXECUTABLE", str(browser))
    monkeypatch.setenv("ODYSSEUS_BROWSER_NO_SANDBOX", "1")
    builtin_mcp = _load_builtin_mcp(monkeypatch)

    assert "--no-sandbox" in builtin_mcp._browser_mcp_args(["--headless"])
