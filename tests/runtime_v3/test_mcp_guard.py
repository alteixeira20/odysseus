import asyncio
import os
import unittest

from src.agent.runtime_v3.mcp_guard import build_mcp_child_env, guarded_call, validate_pinned_npx


class McpGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_environment_does_not_inherit_secrets(self):
        os.environ["ODYSSEUS_TEST_API_KEY"] = "secret"
        child = build_mcp_child_env({"EXPLICIT": "yes"})
        self.assertNotIn("ODYSSEUS_TEST_API_KEY", child)
        self.assertEqual(child["EXPLICIT"], "yes")

    def test_dynamic_npx_install_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_pinned_npx(["-y", "@playwright/mcp@latest"])
        validate_pinned_npx(["@playwright/mcp@0.0.78"])

    async def test_mcp_call_timeout_is_enforced(self):
        async def slow():
            await asyncio.sleep(0.1)
        with self.assertRaises(asyncio.TimeoutError):
            await guarded_call(slow, timeout_seconds=0.01)


if __name__ == "__main__":
    unittest.main()
