from pathlib import Path
import unittest


class OperationsIntegrationSourceTests(unittest.TestCase):
    def test_application_mounts_runtime_router_and_page(self):
        source = Path("app.py").read_text(encoding="utf-8")
        self.assertIn("setup_agent_runtime_routes", source)
        self.assertIn('app.include_router(setup_agent_runtime_routes())', source)
        self.assertIn('@app.get("/agent-runtime")', source)
        self.assertIn('"/api/agent-runs"', source)

    def test_runtime_page_uses_owner_scoped_api_only(self):
        javascript = Path("static/js/agentRuntimeInspector.js").read_text(encoding="utf-8")
        self.assertIn("/api/agent-runs", javascript)
        self.assertNotIn("localStorage", javascript)
        self.assertNotIn("sessionStorage", javascript)
        self.assertNotIn("Authorization", javascript)


if __name__ == "__main__":
    unittest.main()
