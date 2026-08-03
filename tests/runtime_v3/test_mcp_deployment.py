from pathlib import Path
import json
import unittest


class McpDeploymentTests(unittest.TestCase):
    def test_browser_mcp_is_exact_runtime_dependency(self):
        package = json.loads(Path("package.json").read_text(encoding="utf-8"))
        version = package.get("dependencies", {}).get("@playwright/mcp")
        self.assertIsNotNone(version)
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertNotIn("^", version)
        self.assertNotIn("~", version)

    def test_browser_uses_local_binary_without_npx_cache_or_runtime_install(self):
        source = Path("src/builtin_mcp.py").read_text(encoding="utf-8")
        self.assertIn("_find_local_node_binary", source)
        self.assertIn('"mcp-server-playwright"', source)
        self.assertNotIn("@playwright/mcp@latest", source)
        self.assertNotIn("npx -y", source)
        self.assertNotIn("not installed in the npx cache", source)

    def test_windows_node_shim_uses_command_processor(self):
        source = Path("src/builtin_mcp.py").read_text(encoding="utf-8")
        self.assertIn('os.environ.get("COMSPEC", "cmd.exe")', source)
        self.assertIn('["/d", "/s", "/c", executable, *args]', source)

    def test_docker_installs_lockfile_dependencies_before_copying_source(self):
        dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
        manifests = dockerfile.index("COPY package.json package-lock.json")
        install = dockerfile.index("npm ci --omit=dev --ignore-scripts")
        source = dockerfile.index("COPY . .")
        self.assertLess(manifests, install)
        self.assertLess(install, source)
        self.assertIn("node_modules/.bin/mcp-server-playwright", dockerfile)

    def test_native_setup_installs_same_lockfile(self):
        setup = Path("setup.py").read_text(encoding="utf-8")
        self.assertIn("install_node_runtime", setup)
        self.assertIn('"ci", "--omit=dev", "--ignore-scripts"', setup)
        self.assertIn("ODYSSEUS_SKIP_NODE_RUNTIME_INSTALL", setup)
        self.assertIn("mcp-server-playwright", setup)


if __name__ == "__main__":
    unittest.main()
