"""Pull request target tests with a mocked GitHub client."""

import unittest
from unittest import mock

from odytor.errors import FetchError
from odytor.targets.prs import fetch_pull_request


class PullRequestValidationTests(unittest.TestCase):
    def test_issue_number_is_rejected_before_other_pr_fetches(self):
        client = mock.Mock(repo="owner/repo")
        client.api_object.return_value = {"number": 12, "title": "An issue"}

        with self.assertRaises(FetchError) as cm:
            fetch_pull_request(client, 12)

        self.assertIn("is an issue, not a pull request", str(cm.exception))
        client.api_object.assert_called_once_with(
            "repos/owner/repo/issues/12",
            "issue metadata for PR #12 in owner/repo",
        )
        client.api_paginated.assert_not_called()
        client.pr_checks.assert_not_called()


class FetchPullRequestAssemblyTests(unittest.TestCase):
    def test_assembles_pull_request_data_in_order(self):
        client = mock.Mock(repo="owner/repo")
        client.api_object.side_effect = [
            {"number": 12, "title": "A PR", "pull_request": {"url": "u"}},  # issues endpoint
            {"number": 12, "additions": 3},                                 # pulls endpoint
        ]
        client.api_paginated.side_effect = [
            [{"body": "conversation"}],   # issue comments
            [{"state": "APPROVED"}],      # reviews
            [{"path": "a.py"}],           # review comments
            [{"filename": "a.py"}],       # changed files
        ]
        client.pr_checks.return_value = "all green"

        data = fetch_pull_request(client, 12)

        self.assertEqual(data.issue["title"], "A PR")
        self.assertEqual(data.pull_request["additions"], 3)
        self.assertEqual(data.issue_comments, [{"body": "conversation"}])
        self.assertEqual(data.reviews, [{"state": "APPROVED"}])
        self.assertEqual(data.review_comments, [{"path": "a.py"}])
        self.assertEqual(data.files, [{"filename": "a.py"}])
        self.assertEqual(data.checks, "all green")
        # The issues endpoint is consulted before the pulls endpoint.
        self.assertEqual(
            [call.args[0] for call in client.api_object.call_args_list],
            ["repos/owner/repo/issues/12", "repos/owner/repo/pulls/12"],
        )


if __name__ == "__main__":
    unittest.main()
