from __future__ import annotations

from pathlib import Path
import re

ROOT = Path.cwd()


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one exact anchor, found {count}: {old[:100]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def regex_once(path: str, pattern: str, replacement: str, flags: int = 0) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"{path}: expected one regex anchor, found {count}: {pattern[:100]!r}")
    target.write_text(updated, encoding="utf-8")


replace_once("src/agent/rounds/tool_calls.py", "    recover_unknown: bool = True,", "    recover_unknown: bool = False,")
regex_once(
    "src/agent_loop.py",
    r"\n        # ── Fallback: auto-create document if model dumped large code in chat ──.*?\n        # Save cleaned round text",
    "\n        # Save cleaned round text",
    re.S,
)

replace_once(
    "src/mcp_manager.py",
    "from src.runtime_paths import get_app_root\n",
    "from src.runtime_paths import get_app_root\nfrom src.agent.runtime_v3.mcp_guard import build_mcp_child_env, bounded_mcp_result, guarded_call\n",
)
replace_once(
    "src/mcp_manager.py",
    "                env={**os.environ, **env} if env else None,",
    "                env=build_mcp_child_env(env),",
)
regex_once(
    "src/mcp_manager.py",
    r"    async def call_tool\(self, qualified_name: str, arguments: Dict\) -> Dict:.*?\n    async def _reconnect_builtin",
    '''    async def call_tool(self, qualified_name: str, arguments: Dict) -> Dict:\n        """Call one MCP tool exactly once. Transport ambiguity is never replayed."""\n        parts = qualified_name.split("__", 2)\n        if len(parts) != 3 or parts[0] != "mcp":\n            return {"error": f"Invalid MCP tool name: {qualified_name}", "exit_code": 1}\n        server_id, tool_name = parts[1], parts[2]\n        session = self._sessions.get(server_id)\n        if not session:\n            return {"error": f"MCP server not connected: {server_id}", "exit_code": 1}\n        try:\n            result = await guarded_call(lambda: session.call_tool(tool_name, arguments))\n            return bounded_mcp_result(result)\n        except asyncio.TimeoutError:\n            logger.error("MCP tool call timed out without safe retry: %s", qualified_name)\n            return {"error": "MCP call timed out; effect status may be unknown and was not retried", "exit_code": 1, "effect_unknown": True}\n        except asyncio.CancelledError:\n            raise\n        except Exception as exc:\n            logger.error("MCP tool call failed without automatic retry: %s: %s", qualified_name, exc)\n            return {"error": str(exc), "exit_code": 1, "effect_unknown": True}\n\n    async def _reconnect_builtin''',
    re.S,
)

replace_once(
    "src/builtin_mcp.py",
    '"args": ["-y", "@playwright/mcp@latest", "--headless", "--caps", "vision"],',
    '"args": ["@playwright/mcp@0.0.78", "--headless", "--caps", "vision"],',
)
replace_once(
    "src/builtin_mcp.py",
    'BROWSER_MCP_REQUIRE_CACHE = os.environ.get("ODYSSEUS_BROWSER_MCP_REQUIRE_CACHE", "").lower()',
    'BROWSER_MCP_REQUIRE_CACHE = os.environ.get("ODYSSEUS_BROWSER_MCP_REQUIRE_CACHE", "1").lower()',
)
replace_once(
    "src/builtin_mcp.py",
    'os.environ.get("ODYSSEUS_BROWSER_NO_SANDBOX", "1").lower()',
    'os.environ.get("ODYSSEUS_BROWSER_NO_SANDBOX", "0").lower()',
)
replace_once(
    "src/builtin_mcp.py",
    'npx -y @playwright/mcp@latest --version',
    'npx --no-install @playwright/mcp@0.0.78 --version',
)
replace_once(
    "src/builtin_mcp.py",
    "# lets `npx -y` install @playwright/mcp on first start. Locked-down",
    "# requires a pre-cached, exactly pinned @playwright/mcp package. Locked-down",
)
replace_once(
    "src/builtin_mcp.py",
    "# installs can opt back into the old no-network startup behavior",
    "# installs remain no-network at runtime",
)

replace_once(
    "static/js/chat.js",
    "  const RESEARCH_TIMEOUT_MS = 360000;",
    "  const RESEARCH_TIMEOUT_MS = 0; // durable agent/research runs have no destructive browser timer",
)
replace_once(
    "static/js/chat.js",
    "      // Timeout: 6 min for research and agent mode, 3 min otherwise",
    "      // Ordinary chat keeps a client timeout; durable agent/research runs do not.",
)
replace_once(
    "static/js/chat.js",
    "      timeoutId = setTimeout(() => {",
    "      if (timeoutMs > 0) timeoutId = setTimeout(() => {",
)
old_prompt = "Your previous response was interrupted. It ended with:\\n\\n' + cutoff.slice(-500) + '\\n\\nDo NOT repeat what you already said. Continue exactly from where you were cut off."
new_prompt = "Start a recovery turn for the interrupted run. Do not assume prior tool effects were rolled back; continue only from confirmed durable state. Last visible output:\\n\\n' + cutoff.slice(-500)"
chat_path = ROOT / "static/js/chat.js"
chat = chat_path.read_text(encoding="utf-8")
count = chat.count(old_prompt)
if count != 2:
    raise RuntimeError(f"static/js/chat.js: expected two continuation prompts, found {count}")
chat_path.write_text(chat.replace(old_prompt, new_prompt), encoding="utf-8")

replace_once("requirements.txt", "mcp\n", "mcp==1.28.1\n")
replace_once(
    "src/agent/runtime_v3/ledger.py",
    "                _LEDGER.recover_interrupted(stale_after_seconds=0)",
    "                _LEDGER.recover_interrupted(stale_after_seconds=300)",
)

env_path = ROOT / ".env.example"
env_text = env_path.read_text(encoding="utf-8")
block = '''\n# Agent Runtime V3 durability and safety budgets\nODYSSEUS_AGENT_MAX_ROUNDS=200\nODYSSEUS_AGENT_MAX_TOOL_CALLS=256\nODYSSEUS_AGENT_MAX_PROVIDER_CALLS=512\nODYSSEUS_AGENT_MAX_RUN_SECONDS=3600\nODYSSEUS_AGENT_RUN_IDLE_SECONDS=300\nODYSSEUS_AGENT_MAX_EVENT_BYTES=1048576\nODYSSEUS_AGENT_MAX_REPLAY_EVENTS=8192\nODYSSEUS_MCP_CALL_TIMEOUT_SECONDS=120\nODYSSEUS_MCP_MAX_OUTPUT_BYTES=1048576\nODYSSEUS_BROWSER_MCP_REQUIRE_CACHE=1\nODYSSEUS_BROWSER_NO_SANDBOX=0\n'''
if "ODYSSEUS_AGENT_MAX_PROVIDER_CALLS" not in env_text:
    env_path.write_text(env_text.rstrip() + "\n" + block, encoding="utf-8")

print("Runtime V3 integration patches applied")
