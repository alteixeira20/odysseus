from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    target = Path(path)
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one post-patch anchor, found {count}: {old[:160]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


replace_once(
    "src/builtin_mcp.py",
    '"binary": "playwright-mcp",',
    '"binary": "mcp-server-playwright",',
)
replace_once(
    "src/builtin_mcp.py",
    '''                ok = await mcp_manager.connect_server(
                    server_id=server_id,
                    name=cfg["name"],
                    transport="stdio",
                    command=executable,
                    args=args,
                    env=env,
                )
''',
    '''                command = executable
                server_args = args
                if IS_WINDOWS:
                    # npm exposes package bins as .cmd shims on Windows. Launch
                    # through the configured command processor without using a
                    # shell for any untrusted input; executable and arguments
                    # come only from the lockfile-owned built-in definition.
                    command = os.environ.get("COMSPEC", "cmd.exe")
                    server_args = ["/d", "/s", "/c", executable, *args]
                ok = await mcp_manager.connect_server(
                    server_id=server_id,
                    name=cfg["name"],
                    transport="stdio",
                    command=command,
                    args=server_args,
                    env=env,
                )
''',
)
replace_once(
    "setup.py",
    '"playwright-mcp.cmd" if sys.platform == "win32" else "playwright-mcp",',
    '"mcp-server-playwright.cmd" if sys.platform == "win32" else "mcp-server-playwright",',
)
replace_once(
    "setup.py",
    'raise RuntimeError("lockfile install completed but playwright-mcp executable is missing")',
    'raise RuntimeError("lockfile install completed but mcp-server-playwright executable is missing")',
)
replace_once(
    "Dockerfile",
    "&& test -x node_modules/.bin/playwright-mcp \\",
    "&& test -x node_modules/.bin/mcp-server-playwright \\",
)

print("Runtime V3 MCP deployment post-patch corrections applied")
