from pathlib import Path
import re


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one anchor, found {count}: {old[:160]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def regex_once(path: str, pattern: str, replacement: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{path}: expected one regex anchor, found {count}: {pattern[:160]!r}")
    target.write_text(updated, encoding="utf-8")


# Browser MCP resolves only the local lockfile-installed executable.
regex_once(
    "src/builtin_mcp.py",
    r'''def _find_npx\(\) -> str:.*?\n# Server definitions:''',
    '''def _find_local_node_binary(name: str, base_dir: str | None = None) -> str | None:
    """Resolve an executable only from this checkout's node_modules/.bin.

    Global PATH and npm cache state are intentionally ignored so native and
    container launches execute the exact package locked by package-lock.json.
    """
    root = base_dir or get_app_root()
    suffix = ".cmd" if IS_WINDOWS else ""
    candidate = os.path.join(root, "node_modules", ".bin", name + suffix)
    if os.path.isfile(candidate):
        return candidate
    return None

# Server definitions:''',
)
regex_once(
    "src/builtin_mcp.py",
    r'''# NPX-based built-in servers \(run via npx, not Python\).*?BROWSER_MCP_REQUIRE_CACHE = .*?\n''',
    '''# Lockfile-installed Node MCP servers. No package resolution or download is
# permitted while Odysseus is starting.
_BUILTIN_NODE_SERVERS = {
    "builtin_browser": {
        "name": "Built-in: Browser",
        "binary": "playwright-mcp",
        "args": ["--headless", "--caps", "vision"],
    }
}

# Global flag to disable MCP if there are compatibility issues
MCP_DISABLED = os.environ.get("ODYSSEUS_DISABLE_MCP", "").lower() in ("1", "true", "yes")
''',
)
regex_once(
    "src/builtin_mcp.py",
    r'''    # Register NPX-based servers in the background.*\Z''',
    '''    # Register lockfile-installed Node servers after the Python servers.
    async def _start_node_servers():
        await asyncio.sleep(3)
        for server_id, cfg in _BUILTIN_NODE_SERVERS.items():
            executable = _find_local_node_binary(cfg["binary"], base_dir)
            if not executable:
                logger.warning(
                    "%s is unavailable because %s is not installed locally. "
                    "Run `npm ci --omit=dev --ignore-scripts` from %s and restart Odysseus. "
                    "No runtime package download was attempted.",
                    cfg["name"],
                    cfg["binary"],
                    base_dir,
                )
                continue
            args = _browser_mcp_args(cfg["args"]) if server_id == "builtin_browser" else list(cfg["args"])
            logger.info("Starting local Node MCP server: %s", cfg["name"])
            try:
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
                ok = await mcp_manager.connect_server(
                    server_id=server_id,
                    name=cfg["name"],
                    transport="stdio",
                    command=executable,
                    args=args,
                    env=env,
                )
                if ok:
                    logger.info("Built-in Node MCP server registered: %s", cfg["name"])
                else:
                    logger.warning("Built-in Node MCP server failed to connect: %s", cfg["name"])
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                logger.warning(
                    "Built-in Node MCP server %s error: %s: %s",
                    cfg["name"],
                    type(exc).__name__,
                    exc,
                )

    _spawn_bg(_start_node_servers())
''',
)

# Remove obsolete imports used only by the npx-cache probe.
replace_once("src/builtin_mcp.py", "import json\n", "")
replace_once("src/builtin_mcp.py", "import subprocess\n", "")
replace_once(
    "src/mcp_manager.py",
    "npx --no-install @playwright/mcp@0.0.78 --version",
    "npm ci --omit=dev --ignore-scripts",
)

# Native setup installs the same exact lockfile as Docker.
replace_once(
    "setup.py",
    '''def configure_environment():
''',
    '''def install_node_runtime():
    """Install exact Node runtime dependencies from package-lock.json."""
    if os.environ.get("ODYSSEUS_SKIP_NODE_RUNTIME_INSTALL", "").lower() in {"1", "true", "yes"}:
        print("Skipping Node runtime install (ODYSSEUS_SKIP_NODE_RUNTIME_INSTALL is set)")
        return False
    npm = shutil.which("npm")
    if not npm:
        print("Warning: npm is unavailable; browser MCP will remain disabled.")
        print("Install Node.js 20+ and rerun: npm ci --omit=dev --ignore-scripts")
        return False
    lockfile = os.path.join(BASE_DIR, "package-lock.json")
    if not os.path.isfile(lockfile):
        raise RuntimeError("package-lock.json is required for deterministic Node runtime setup")
    print("Installing lockfile-pinned Node runtime dependencies...")
    subprocess.run(
        [npm, "ci", "--omit=dev", "--ignore-scripts"],
        cwd=BASE_DIR,
        check=True,
        timeout=900,
    )
    binary = os.path.join(
        BASE_DIR,
        "node_modules",
        ".bin",
        "playwright-mcp.cmd" if sys.platform == "win32" else "playwright-mcp",
    )
    if not os.path.isfile(binary):
        raise RuntimeError("lockfile install completed but playwright-mcp executable is missing")
    print("Pinned browser MCP runtime installed.")
    return True


def configure_environment():
''',
)
replace_once(
    "setup.py",
    '''    # Create necessary directories
    create_directories()

    # Configure environment
''',
    '''    # Create necessary directories
    create_directories()

    # Install the exact Node MCP runtime. This is setup-time network access;
    # application startup never downloads executable code.
    install_node_runtime()

    # Configure environment
''',
)

# Docker installs exactly the same lockfile before source copy.
replace_once(
    "Dockerfile",
    '''WORKDIR /app

# System libraries required by PyTorch, OpenCV, OCR, PDF processing,
''',
    '''WORKDIR /app

# Install the exact Node MCP runtime before copying application source so this
# layer is cacheable and startup never resolves packages from the network.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \\
    && test -x node_modules/.bin/playwright-mcp \\
    && npm cache clean --force

# System libraries required by PyTorch, OpenCV, OCR, PDF processing,
''',
)

# Documentation and environment defaults match the local-install model.
replace_once(
    "docs/setup.md",
    '''## Prerequisites

- Python 3.10+
- Linux/macOS (Windows via WSL2 recommended)
- 8 GB RAM minimum (16 GB recommended)
- 20 GB free disk space
''',
    '''## Prerequisites

- Python 3.10+
- Node.js 20+ with npm (required for the pinned browser MCP runtime)
- Linux/macOS (Windows via WSL2 recommended)
- 8 GB RAM minimum (16 GB recommended)
- 20 GB free disk space
''',
)
replace_once(
    "docs/setup.md",
    '''pip install --upgrade pip
pip install -r requirements.txt
python setup.py
''',
    '''pip install --upgrade pip
pip install -r requirements.txt
python setup.py  # also runs npm ci from package-lock.json
''',
)
replace_once(
    ".env.example",
    "ODYSSEUS_BROWSER_MCP_REQUIRE_CACHE=1\n",
    "# Browser MCP must exist in node_modules from the exact package lock.\n",
)

print("Runtime V3 MCP deployment integration applied")
