"""Path classification tests for analysis.changed_paths."""

import unittest

from odytor.analysis import changed_paths as cp


class HelperTests(unittest.TestCase):
    def test_is_docs(self):
        self.assertTrue(cp.is_docs("README.md"))
        self.assertTrue(cp.is_docs("docs/guide.rst"))
        self.assertFalse(cp.is_docs("app/main.py"))

    def test_is_test(self):
        self.assertTrue(cp.is_test("tests/test_main.py"))
        self.assertTrue(cp.is_test("src/widget_test.py"))
        self.assertTrue(cp.is_test("spec/foo_spec.py"))
        self.assertFalse(cp.is_test("app/main.py"))


class ReviewFlagTests(unittest.TestCase):
    def assertFlag(self, paths, expected):
        self.assertIn(expected, cp.review_flags(paths))

    def test_docs_only(self):
        self.assertEqual(cp.review_flags(["README.md", "docs/guide.md"]), ["docs-only"])

    def test_tests_only(self):
        self.assertEqual(cp.review_flags(["tests/test_a.py", "src/b_test.py"]), ["tests-only"])

    def test_ci_tooling(self):
        self.assertFlag([".github/workflows/ci.yml"], "CI/tooling")
        self.assertFlag(["docker-compose.yml"], "CI/tooling")

    def test_ui_static_frontend(self):
        self.assertFlag(["frontend/app.js"], "UI paths")
        self.assertFlag(["static/css/site.css"], "UI paths")
        self.assertFlag(["assets/theme.scss"], "UI paths")

    def test_security(self):
        self.assertFlag(["config/credentials.py"], "security-sensitive")

    def test_auth(self):
        self.assertFlag(["core/auth/oauth.py"], "auth paths")

    def test_session(self):
        self.assertFlag(["web/session.py"], "session paths")

    def test_owner_scope_counts_as_auth(self):
        self.assertFlag(["core/owner_scope.py"], "auth paths")

    def test_database_migrations_storage(self):
        self.assertFlag(["migrations/0001_init.py"], "database paths")
        self.assertFlag(["alembic/versions/abc.py"], "database paths")
        self.assertFlag(["core/storage/blobs.py"], "database paths")

    def test_provider_model_endpoints(self):
        self.assertFlag(["providers/openai_client.py"], "provider/model paths")
        self.assertFlag(["api/endpoints/chat.py"], "provider/model paths")

    def test_packaging_dependencies(self):
        self.assertFlag(["requirements.txt"], "packaging/dependencies")
        self.assertFlag(["pyproject.toml"], "packaging/dependencies")
        self.assertFlag(["package.json"], "packaging/dependencies")

    def test_mixed_changes(self):
        flags = cp.review_flags(["core/auth/login.py", "frontend/app.js", "requirements.txt"])
        self.assertIn("auth paths", flags)
        self.assertIn("UI paths", flags)
        self.assertIn("packaging/dependencies", flags)
        self.assertNotIn("docs-only", flags)
        self.assertNotIn("tests-only", flags)

    def test_empty_paths(self):
        self.assertEqual(cp.review_flags([]), [])


if __name__ == "__main__":
    unittest.main()
