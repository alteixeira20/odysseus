"""Formatting tests: section headers, deterministic JSON, label rendering."""

import json
import unittest
from pathlib import Path

from odytor import formatters
from odytor.models import PullRequestData
from odytor.targets.labels import format_labels

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class SectionHeaderTests(unittest.TestCase):
    def test_section_header_structure(self):
        lines = formatters.section("TITLE").split("\n")
        self.assertEqual(lines[0], "=" * 100)
        self.assertEqual(lines[1], "TITLE")
        self.assertEqual(lines[2], "=" * 100)

    def test_subsection_uses_dashes(self):
        text = formatters.subsection("SUB")
        self.assertTrue(text.startswith("\n"))
        self.assertIn("-" * 100, text)
        self.assertIn("SUB", text)


class RenderJsonTests(unittest.TestCase):
    def test_render_json_is_deterministic(self):
        data = {"b": 1, "a": 2, "nested": {"y": 1, "x": 2}}
        self.assertEqual(formatters.render_json(data), formatters.render_json(data))

    def test_render_json_preserves_key_order_and_indent(self):
        rendered = formatters.render_json({"b": 1, "a": 2})
        self.assertEqual(rendered, json.dumps({"b": 1, "a": 2}, indent=2, ensure_ascii=False))

    def test_render_json_keeps_unicode(self):
        self.assertIn("café", formatters.render_json({"name": "café"}))


class CompactIssueTests(unittest.TestCase):
    def test_compact_issue_classifies_issue(self):
        compact = formatters.compact_issue(load("issue_2523_metadata.json"))
        self.assertEqual(compact["number"], 2523)
        self.assertEqual(compact["type"], "Issue")
        self.assertEqual(compact["author"], "someuser")
        self.assertIn("bug", compact["labels"])

    def test_compact_issue_detects_pull_request(self):
        compact = formatters.compact_issue({"number": 1, "pull_request": {"url": "x"}})
        self.assertEqual(compact["type"], "PR")


class PullRequestHeadingTests(unittest.TestCase):
    def test_pr_uses_conversation_headings(self):
        data = PullRequestData(
            issue={"number": 1, "body": "body", "pull_request": {"url": "x"}},
            issue_comments=[],
            pull_request={},
            reviews=[],
            review_comments=[],
            files=[],
            checks="No checks reported.",
        )
        text = formatters.format_pull_request(1, data)
        self.assertIn("PR #1 - CONVERSATION METADATA", text)
        self.assertIn("PR #1 - CONVERSATION COMMENTS", text)
        self.assertIn("PR #1 - PULL REQUEST METADATA", text)
        self.assertNotIn("PR #1 - ISSUE METADATA", text)


class LabelRenderingTests(unittest.TestCase):
    def test_format_labels_lists_each_label(self):
        text = format_labels("owner/repo", load("labels.json"))
        self.assertIn("LABELS - owner/repo", text)
        self.assertIn("total labels: 3", text)
        self.assertIn("bug", text)
        self.assertIn("#d73a4a", text)
        self.assertIn("Improvements or additions to documentation", text)


if __name__ == "__main__":
    unittest.main()
