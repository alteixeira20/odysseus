"""Comment limiting and ordering tests for the print renderers."""

import unittest

from odytor import formatters as F
from odytor.models import CommentWindow, DiscussionData, IssueData


def issue_comments(n):
    return [
        {"created_at": f"2026-06-{i:02d}T00:00:00Z", "user": {"login": f"u{i}"}, "body": f"c{i}"}
        for i in range(1, n + 1)
    ]


def discussion_comments(n):
    return [
        {"author": {"login": f"u{i}"}, "body": f"d{i}",
         "createdAt": f"2026-06-{i:02d}T00:00:00Z", "replies": []}
        for i in range(1, n + 1)
    ]


class ApplyWindowTests(unittest.TestCase):
    def test_default_window_is_noop(self):
        comments = issue_comments(3)
        shown, total = F.apply_comment_window(comments, CommentWindow())
        self.assertEqual(shown, comments)
        self.assertEqual(total, 3)

    def test_latest_reverses(self):
        shown, total = F.apply_comment_window(issue_comments(3), CommentWindow(order="latest"))
        self.assertEqual([c["body"] for c in shown], ["c3", "c2", "c1"])
        self.assertEqual(total, 3)

    def test_limit_keeps_first_of_chosen_order(self):
        shown, _ = F.apply_comment_window(issue_comments(5), CommentWindow(limit=2, order="latest"))
        self.assertEqual([c["body"] for c in shown], ["c5", "c4"])
        oldest, _ = F.apply_comment_window(issue_comments(5), CommentWindow(limit=2, order="oldest"))
        self.assertEqual([c["body"] for c in oldest], ["c1", "c2"])

    def test_header_only_when_customized(self):
        self.assertEqual(F.comment_window_header(3, 3, CommentWindow()), "")
        header = F.comment_window_header(2, 5, CommentWindow(limit=2, order="latest"))
        self.assertIn("Comments displayed: 2 of 5", header)
        self.assertIn("Order: latest", header)


class IssueRenderTests(unittest.TestCase):
    def setUp(self):
        self.data = IssueData(
            metadata={"number": 1, "title": "t", "body": "ISSUE-BODY"},
            comments=issue_comments(5),
        )

    def test_default_output_has_no_window_header(self):
        out = F.format_issue(1, self.data)
        self.assertNotIn("Comments displayed", out)
        self.assertIn("ISSUE-BODY", out)

    def test_latest_limit_shows_window_and_body(self):
        out = F.format_issue(1, self.data, CommentWindow(limit=2, order="latest"))
        self.assertIn("ISSUE-BODY", out)
        self.assertIn("Comments displayed: 2 of 5", out)
        self.assertIn("Order: latest", out)
        self.assertIn("c5", out)
        self.assertNotIn("c1", out)
        self.assertLess(out.index("c5"), out.index("c4"))

    def test_oldest_limit_is_chronological(self):
        out = F.format_issue(1, self.data, CommentWindow(limit=2, order="oldest"))
        self.assertIn("c1", out)
        self.assertNotIn("c5", out)
        self.assertLess(out.index("c1"), out.index("c2"))


class DiscussionRenderTests(unittest.TestCase):
    def setUp(self):
        self.data = DiscussionData(
            metadata={"number": 9, "title": "T", "body": "MAIN-POST"},
            comments=discussion_comments(12),
        )

    def test_body_always_first_then_latest_window(self):
        out = F.format_discussion(9, self.data, CommentWindow(limit=10, order="latest"))
        self.assertIn("MAIN-POST", out)
        self.assertLess(out.index("MAIN-POST"), out.index("u12"))
        self.assertIn("Comments displayed: 10 of 12", out)
        self.assertIn("Order: latest", out)

    def test_latest_10_excludes_two_oldest(self):
        out = F.format_discussion(9, self.data, CommentWindow(limit=10, order="latest"))
        # newest 10 are u12..u3; the two oldest (u1, u2) are excluded.
        self.assertIn("d12", out)
        self.assertNotIn("d1\n", out)
        self.assertNotIn("d2\n", out)

    def test_default_shows_all_no_header(self):
        out = F.format_discussion(9, self.data)
        self.assertNotIn("Comments displayed", out)
        self.assertIn("d1", out)
        self.assertIn("d12", out)


if __name__ == "__main__":
    unittest.main()
