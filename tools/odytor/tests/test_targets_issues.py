"""Issue target tests with a mocked GitHub client."""

import unittest
from unittest import mock

from odytor import commands
from odytor.errors import FetchError
from odytor.models import Target
from odytor.progress import disabled
from odytor.targets.issues import fetch_issue


class IssueValidationTests(unittest.TestCase):
    def test_regular_issue_fetches_comments(self):
        client = mock.Mock(repo="owner/repo")
        client.api_object.return_value = {"number": 12, "title": "An issue"}
        client.api_paginated.return_value = [{"body": "comment"}]

        data = fetch_issue(client, 12)

        self.assertEqual(data.metadata["number"], 12)
        self.assertEqual(data.comments, [{"body": "comment"}])

    def test_pull_request_number_is_rejected_before_comments(self):
        client = mock.Mock(repo="owner/repo")
        client.api_object.return_value = {
            "number": 12,
            "title": "A pull request",
            "pull_request": {"url": "https://api.github.com/pulls/12"},
        }

        with self.assertRaises(FetchError) as cm:
            fetch_issue(client, 12)

        self.assertIn("is a pull request, not an issue", str(cm.exception))
        self.assertIn("Use --pr 12", str(cm.exception))
        client.api_paginated.assert_not_called()


class IssueCommandRejectionTests(unittest.TestCase):
    """Both --print --issue and --review --issue must reject PR metadata."""

    def _pr_metadata_client(self):
        client = mock.Mock(repo="owner/repo")
        client.api_object.return_value = {
            "number": 12,
            "title": "A pull request",
            "pull_request": {"url": "https://api.github.com/pulls/12"},
        }
        return client

    def test_print_issue_rejects_pr_before_rendering(self):
        client = self._pr_metadata_client()
        target = Target(kind="issue", number=12)
        with mock.patch.object(commands, "format_issue") as render:
            with self.assertRaises(FetchError):
                commands.run_print(
                    client, target, mock.Mock(), disabled()
                )
        render.assert_not_called()
        client.api_paginated.assert_not_called()

    def test_review_issue_rejects_pr_before_rendering(self):
        client = self._pr_metadata_client()
        target = Target(kind="issue", number=12)
        with mock.patch.object(
            commands.review_summary, "format_issue_review"
        ) as render:
            with self.assertRaises(FetchError):
                commands.run_review(client, target, disabled())
        render.assert_not_called()
        client.api_paginated.assert_not_called()


if __name__ == "__main__":
    unittest.main()
