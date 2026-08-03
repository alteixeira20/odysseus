import os
from pathlib import Path
import tempfile
import unittest

from src.builtin_mcp import _find_local_node_binary


class LocalNodeBinaryTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX npm symlink behavior")
    def test_accepts_npm_bin_symlink_target_inside_node_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "node_modules" / ".bin"
            package_dir = root / "node_modules" / "@playwright" / "mcp"
            bin_dir.mkdir(parents=True)
            package_dir.mkdir(parents=True)
            target = package_dir / "cli.js"
            target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            target.chmod(0o755)
            shim = bin_dir / "playwright-mcp"
            shim.symlink_to(Path("../@playwright/mcp/cli.js"))

            self.assertEqual(
                _find_local_node_binary("playwright-mcp", str(root)),
                str(shim),
            )

    @unittest.skipIf(os.name == "nt", "POSIX symlink escape behavior")
    def test_rejects_npm_bin_symlink_target_outside_node_modules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "node_modules" / ".bin"
            bin_dir.mkdir(parents=True)
            outside = root / "outside.js"
            outside.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            outside.chmod(0o755)
            shim = bin_dir / "playwright-mcp"
            shim.symlink_to(Path("../../outside.js"))

            self.assertIsNone(
                _find_local_node_binary("playwright-mcp", str(root))
            )

    def test_connection_error_guidance_never_invokes_npx(self):
        source = Path("src/mcp_manager.py").read_text(encoding="utf-8")
        self.assertNotIn("npx --no-install @playwright/mcp", source)
        self.assertIn("node_modules/.bin/playwright-mcp", source)
        self.assertIn("Application startup will not download", source)


if __name__ == "__main__":
    unittest.main()
