import asyncio
from unittest.mock import patch

from src.mcp_manager import _format_mcp_connection_error, McpManager


def test_playwright_mcp_connection_error_includes_lockfile_install_hint():
    msg = _format_mcp_connection_error(
        "Built-in: Browser",
        "/app/node_modules/.bin/playwright-mcp",
        ["--headless"],
        RuntimeError("package not found"),
    )

    assert "package not found" in msg
    assert "lockfile-installed Browser MCP runtime could not start" in msg
    assert "npm ci --omit=dev --ignore-scripts" in msg
    assert "node_modules/.bin/playwright-mcp" in msg
    assert "restart Odysseus" in msg
    assert "npx -y" not in msg


def test_generic_mcp_connection_error_preserves_original_error():
    msg = _format_mcp_connection_error(
        "Custom MCP",
        "python",
        ["server.py"],
        RuntimeError("boom"),
    )

    assert msg == "boom"


def test_http_transport_routes_to_start_http_connect():
    mgr = McpManager()

    async def fake_start(server_id, name, url):
        return "ROUTED"

    with patch.object(McpManager, "_start_http_connect", side_effect=fake_start) as m:
        result = asyncio.run(mgr.connect_server("id1", "n", "http", url="https://x/mcp"))
    assert result == "ROUTED"
    m.assert_called_once()
