from pathlib import Path
import unittest


class WorkspaceJournalIntegrationSourceTests(unittest.TestCase):
    def test_patch_tool_uses_runtime_v3_journal(self):
        source = Path("src/agent_tools/filesystem_tools.py").read_text(encoding="utf-8")
        self.assertIn("get_workspace_journal().commit(prepared)", source)
        self.assertIn('"crash_atomic": True', source)
        self.assertIn('"recovery_semantics": "all_old_or_all_new"', source)
        self.assertNotIn('"transaction": "staged_with_best_effort_rollback"', source)

    def test_startup_recovers_before_background_services_start(self):
        source = Path("app.py").read_text(encoding="utf-8")
        recovery = source.index("get_workspace_journal().recover")
        background = source.index("start_bg_monitor")
        self.assertLess(recovery, background)
        self.assertIn("Workspace transaction recovery failed; refusing startup", source)


if __name__ == "__main__":
    unittest.main()
