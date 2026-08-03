"""Auto-registration of built-in MCP servers.

Python servers execute from this checkout. Node-based servers execute only from
``node_modules/.bin`` populated by the repository lockfile; startup never asks
npm/npx to resolve or download executable code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys

from core.platform_compat import IS_WINDOWS
from src.runtime_paths import get_app_root

logger = logging.getLogger(__name__)


_BUILTIN_SERVERS = {
    "image_gen": ("mcp_servers/image_gen_server.py", "Built-in: Image Generation"),
    "memory": ("mcp_servers/memory_server.py", "Built-in: Memory"),
    "rag": ("mcp_servers/rag_server.py", "Built-in: RAG"),
    "email": ("mcp_servers/email_server.py", "Built-in: Email"),
}

_BUILTIN_NODE_SERVERS = {
    "builtin_browser": {
        "name": "Built-in: Browser",
        "binary": "mcp-server-playwright",
        "args": ["--headless", "--caps", "vision"],
    }
}

MCP_DISABLED = os.environ.get("ODYSSEUS_DISABLE_MCP", "").lower() in {
    "1",
    "true",
    "yes",
}

_BG_TASKS: set[asyncio.Task] = set()


def _spawn_bg(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


def _find_local_node_binary(name: str, base_dir: str | None = None) -> str | None:
    """Resolve a package binary only from this checkout's node_modules.

    Global PATH, npm caches, and globally installed packages are intentionally
    ignored. This makes the package-lock file the executable trust boundary.
    """
    root = os.path.realpath(base_dir or get_app_root())
    bin_dir = os.path.realpath(os.path.join(root, "node_modules", ".bin"))
    suffix = ".cmd" if IS_WINDOWS else ""
    candidate = os.path.realpath(os.path.join(bin_dir, name + suffix))
    try:
        if os.path.commonpath((bin_dir, candidate)) != bin_dir:
            return None
    except ValueError:
        return None
    return candidate if os.path.isfile(candidate) else None


def _find_browser_executable() -> str:
    configured = os.environ.get("ODYSSEUS_BROWSER_EXECUTABLE", "").strip()
    if configured:
        configured = os.path.realpath(os.path.expanduser(configured))
        if os.path.isfile(configured):
            return configured
        logger.warning("Configured browser executable does not exist: %s", configured)
        return ""
    for name in ("google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    for candidate in (
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if os.path.isfile(candidate):
            return candidate
    return ""


def _browser_mcp_args(args: list[str]) -> list[str]:
    out = list(args or ())
    if "--executable-path" not in out:
        browser = _find_browser_executable()
        if browser:
            out.extend(["--executable-path", browser])
    if os.environ.get("ODYSSEUS_BROWSER_ISOLATED", "1").lower() not in {
        "0",
        "false",
        "no",
    }:
        if "--isolated" not in out and "--user-data-dir" not in out:
            out.append("--isolated")
    if os.environ.get("ODYSSEUS_BROWSER_NO_SANDBOX", "0").lower() not in {
        "0",
        "false",
        "no",
    }:
        if "--no-sandbox" not in out and "--sandbox" not in out:
            out.append("--no-sandbox")
    return out


def builtin_python_env(base_dir: str) -> dict[str, str]:
    existing = os.environ.get("PYTHONPATH", "")
    parts = [base_dir]
    for item in existing.split(os.pathsep):
        if item and item not in parts:
            parts.append(item)
    return {"PYTHONPATH": os.pathsep.join(parts)}


async def register_builtin_servers(mcp_manager) -> None:
    if MCP_DISABLED:
        logger.info("Built-in MCP servers disabled via ODYSSEUS_DISABLE_MCP")
        return

    base_dir = get_app_root()
    python = sys.executable

    async def connect_python_server(server_id: str, script_path: str, name: str) -> None:
        try:
            ok = await mcp_manager.connect_server(
                server_id=server_id,
                name=name,
                transport="stdio",
                command=python,
                args=[script_path],
                env=builtin_python_env(base_dir),
            )
            if ok:
                logger.info("Built-in MCP server registered: %s", name)
            else:
                logger.warning("Built-in MCP server failed to connect: %s", name)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            logger.warning(
                "Built-in MCP server %s error: %s: %s",
                name,
                type(exc).__name__,
                exc,
            )

    for server_id, (script, name) in _BUILTIN_SERVERS.items():
        script_path = os.path.join(base_dir, script)
        if not os.path.isfile(script_path):
            logger.warning("Built-in MCP server script not found: %s", script_path)
            continue
        _spawn_bg(connect_python_server(server_id, script_path, name))

    async def start_node_servers() -> None:
        await asyncio.sleep(3)
        for server_id, config in _BUILTIN_NODE_SERVERS.items():
            executable = _find_local_node_binary(config["binary"], base_dir)
            if not executable:
                logger.warning(
                    "%s is unavailable because %s is not installed from package-lock.json. "
                    "Run `npm ci --omit=dev --ignore-scripts` in %s and restart. "
                    "No runtime package download was attempted.",
                    config["name"],
                    config["binary"],
                    base_dir,
                )
                continue

            args = (
                _browser_mcp_args(config["args"])
                if server_id == "builtin_browser"
                else list(config["args"])
            )
            env = None
            if server_id == "builtin_browser":
                cache_home = os.environ.get(
                    "ODYSSEUS_BROWSER_MCP_CACHE",
                    os.path.join(base_dir, "data", "local", "playwright-mcp-cache"),
                )
                os.makedirs(cache_home, exist_ok=True)
                env = {
                    "XDG_CACHE_HOME": cache_home,
                    "PLAYWRIGHT_BROWSERS_PATH": os.path.join(cache_home, "browsers"),
                }

            command = executable
            server_args = args
            if IS_WINDOWS:
                command = os.environ.get("COMSPEC", "cmd.exe")
                server_args = ["/d", "/s", "/c", executable, *args]

            try:
                ok = await mcp_manager.connect_server(
                    server_id=server_id,
                    name=config["name"],
                    transport="stdio",
                    command=command,
                    args=server_args,
                    env=env,
                )
                if ok:
                    logger.info("Built-in Node MCP server registered: %s", config["name"])
                else:
                    logger.warning(
                        "Built-in Node MCP server failed to connect: %s",
                        config["name"],
                    )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                logger.warning(
                    "Built-in Node MCP server %s error: %s: %s",
                    config["name"],
                    type(exc).__name__,
                    exc,
                )

    _spawn_bg(start_node_servers())
