from pathlib import Path

path = Path("src/mcp_manager.py")
text = path.read_text(encoding="utf-8")
old = '''    if "@playwright/mcp" in lower_command:
        return (
            f"{raw_error}\\n\\n"
            "Browser MCP could not start. On fresh installs, cache the Playwright MCP package once before connecting:\\n\\n"
            "npx --no-install @playwright/mcp@0.0.78 --version\\n\\n"
            "Then restart Odysseus and reconnect the Browser MCP server."
        )
'''
new = '''    if "playwright-mcp" in lower_command:
        return (
            f"{raw_error}\\n\\n"
            "The lockfile-installed Browser MCP runtime could not start. "
            "Run `npm ci --omit=dev --ignore-scripts` in the Odysseus application root, "
            "verify `node_modules/.bin/playwright-mcp` exists, and restart Odysseus. "
            "Application startup will not download or resolve MCP executable code."
        )
'''
if text.count(old) != 1:
    raise RuntimeError("Playwright MCP connection-error anchor changed")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
print("Removed obsolete npx Browser MCP recovery guidance")
