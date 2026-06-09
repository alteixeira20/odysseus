"""Review-prep summary tests. Uses fixtures and synthetic views; no network."""

import json
import shlex
import unittest
from pathlib import Path

from odytor.analysis import review_summary as rs
from odytor.models import IssueData

FIXTURES = Path(__file__).resolve().parent / "fixtures"
VERDICT_PHRASES = ("do not merge", "merge this pr", "approve this", "reject this")


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def pr_view():
    view = load("pr_3128_metadata.json")
    view["files"] = load("pr_3128_files.json")
    return view


class PrReviewTests(unittest.TestCase):
    def setUp(self):
        self.text = rs.format_pr_review(3128, pr_view())

    def test_includes_target_branch(self):
        self.assertIn("base: dev", self.text)

    def test_includes_draft_status(self):
        self.assertIn("draft: False", self.text)

    def test_includes_mergeability(self):
        self.assertIn("mergeable: MERGEABLE", self.text)
        self.assertIn("mergeStateStatus: BEHIND", self.text)

    def test_includes_labels(self):
        self.assertIn("ready for review", self.text)

    def test_includes_changed_file_summary(self):
        self.assertIn("CHANGED FILES (1)", self.text)
        self.assertIn(".github/pull_request_review_template.md", self.text)

    def test_includes_checks_summary(self):
        self.assertIn("total checks: 4", self.text)
        self.assertIn("all reported checks passed", self.text)

    def test_includes_risk_flags(self):
        self.assertIn("REVIEW-RISK FLAGS", self.text)
        self.assertIn("docs-only", self.text)
        self.assertIn("CI/tooling", self.text)

    def test_includes_suggested_validation(self):
        self.assertIn("SUGGESTED LOCAL VALIDATION", self.text)
        self.assertIn("(suggestions only", self.text)

    def test_no_automatic_verdict_wording(self):
        lowered = self.text.lower()
        for phrase in VERDICT_PHRASES:
            self.assertNotIn(phrase, lowered)
        self.assertIn("not a verdict", lowered)


class PrReviewSignalTests(unittest.TestCase):
    def _view(self, files, rollup=None):
        base = load("pr_3128_metadata.json")
        base["files"] = files
        base["statusCheckRollup"] = rollup if rollup is not None else []
        return base

    def test_failing_check_is_surfaced(self):
        rollup = [{"name": "unit", "status": "COMPLETED", "conclusion": "FAILURE"}]
        text = rs.format_pr_review(1, self._view([], rollup))
        self.assertIn("FAILING: unit", text)

    def test_security_file_flags_and_py_compile_suggestion(self):
        files = [{"path": "core/auth/login.py", "additions": 5, "deletions": 1}]
        text = rs.format_pr_review(2, self._view(files))
        self.assertIn("auth paths", text)
        self.assertIn("python3 -m py_compile -- core/auth/login.py", text)

    def test_python_validation_quotes_spaces_and_shell_metacharacters(self):
        paths = ["safe path.py", "src/unsafe;$(touch nope)`echo bad`.py"]
        suggestions = rs.suggested_validations(paths)
        command = next(
            line.strip()
            for suggestion in suggestions
            for line in suggestion.splitlines()
            if line.strip().startswith("python3 -m py_compile")
        )
        self.assertEqual(
            shlex.split(command),
            ["python3", "-m", "py_compile", "--", *paths],
        )
        self.assertIn("'safe path.py'", command)
        self.assertIn("'src/unsafe;$(touch nope)`echo bad`.py'", command)

    def test_python_option_like_path_is_after_double_dash(self):
        command = rs.suggested_validations(["-danger.py"])[0].splitlines()[-1]
        self.assertEqual(
            shlex.split(command),
            ["python3", "-m", "py_compile", "--", "-danger.py"],
        )

    def test_javascript_option_like_path_is_after_double_dash(self):
        command = rs.suggested_validations(["-danger.js"])[0].splitlines()[-1]
        self.assertEqual(
            shlex.split(command),
            ["node", "--check", "--", "-danger.js"],
        )

    def test_javascript_flag_like_path_is_after_double_dash(self):
        # A path that collides with node's own --check flag must stay a file arg.
        command = rs.suggested_validations(["--check.js"])[0].splitlines()[-1]
        self.assertEqual(
            shlex.split(command),
            ["node", "--check", "--", "--check.js"],
        )

    def test_javascript_validation_quotes_spaces_and_shell_metacharacters(self):
        path = "src/unsafe;$(touch nope)`echo bad`.js"
        command = rs.suggested_validations([path])[0].splitlines()[-1]
        self.assertEqual(
            shlex.split(command),
            ["node", "--check", "--", path],
        )
        self.assertIn("'src/unsafe;$(touch nope)`echo bad`.js'", command)

    def test_capped_javascript_command_has_separate_omission_note(self):
        paths = [f"src/file {index}.js" for index in range(rs.COMMAND_PATH_CAP + 3)]
        suggestions = rs.suggested_validations(paths)
        js_commands = [
            line.strip()
            for suggestion in suggestions
            for line in suggestion.splitlines()
            if line.strip().startswith("node --check")
        ]
        self.assertEqual(len(js_commands), rs.COMMAND_PATH_CAP)
        self.assertIn(
            "Additional JavaScript files omitted from the copyable command: 3",
            suggestions,
        )

    def test_capped_python_command_has_separate_omission_note(self):
        paths = [f"src/file {index}.py" for index in range(rs.COMMAND_PATH_CAP + 4)]
        suggestions = rs.suggested_validations(paths)
        command = next(
            line.strip()
            for suggestion in suggestions
            for line in suggestion.splitlines()
            if line.strip().startswith("python3 -m py_compile")
        )
        self.assertNotIn("...(+", command)
        self.assertEqual(len(shlex.split(command)), 4 + rs.COMMAND_PATH_CAP)
        self.assertIn(
            "Additional Python files omitted from the copyable command: 4",
            suggestions,
        )

    def test_stale_label_is_reported_as_existing_review_state(self):
        view = self._view([])
        view["labels"] = [{"name": "stale pr"}]
        text = rs.format_pr_review(3, view)
        self.assertIn("review-state label: stale pr", text)
        self.assertNotIn("labeled stale", text)


class IssueReviewTests(unittest.TestCase):
    def test_issue_review_summary(self):
        data = IssueData(metadata=load("issue_2523_metadata.json"), comments=[])
        text = rs.format_issue_review(2523, data)
        self.assertIn("REVIEW PREP - ISSUE #2523", text)
        self.assertIn("export fails on empty session", text)
        self.assertIn("not a verdict", text.lower())


if __name__ == "__main__":
    unittest.main()
