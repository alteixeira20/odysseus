"""GitHubClient tests with the subprocess runner mocked. No live gh calls."""

import unittest
from unittest import mock

from odytor import gh_client
from odytor.errors import CommandError, FetchError
from odytor.gh_client import GitHubClient
from odytor.models import CommandResult


def result(stdout="", stderr="", code=0):
    return CommandResult(stdout=stdout, stderr=stderr, returncode=code)


class ApiObjectTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient("owner/repo")

    def test_successful_json_object(self):
        with mock.patch.object(gh_client, "run_command", return_value=result('{"number": 1}')):
            data = self.client.api_object("repos/owner/repo/issues/1", "issue #1")
        self.assertEqual(data, {"number": 1})

    def test_invalid_json_raises_fetch_error(self):
        with mock.patch.object(gh_client, "run_command", return_value=result("{not json")):
            with self.assertRaises(FetchError):
                self.client.api_object("repos/owner/repo/issues/1", "issue #1")

    def test_non_object_payload_raises_fetch_error(self):
        with mock.patch.object(gh_client, "run_command", return_value=result("[1, 2, 3]")):
            with self.assertRaises(FetchError) as cm:
                self.client.api_object("repos/owner/repo/issues/1", "issue #1")
        self.assertIn("Expected an object", str(cm.exception))

    def test_command_failure_has_clean_message(self):
        with mock.patch.object(gh_client, "run_command", return_value=result(stderr="boom", code=1)):
            with self.assertRaises(CommandError) as cm:
                self.client.api_object("repos/owner/repo/issues/1", "issue #1 in owner/repo")
        message = str(cm.exception)
        self.assertIn("Could not fetch issue #1 in owner/repo", message)
        self.assertIn("boom", message)


class ApiPaginatedTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient("owner/repo")

    def test_parses_concatenated_pages(self):
        stream = '[{"x": 1}]\n[{"x": 2}, {"x": 3}]'
        with mock.patch.object(gh_client, "run_command", return_value=result(stream)):
            items = self.client.api_paginated("repos/owner/repo/labels", "labels")
        self.assertEqual(items, [{"x": 1}, {"x": 2}, {"x": 3}])

    def test_failure_raises_command_error(self):
        with mock.patch.object(gh_client, "run_command", return_value=result(stderr="nope", code=1)):
            with self.assertRaises(CommandError) as cm:
                self.client.api_paginated("repos/owner/repo/labels", "labels for owner/repo")
        self.assertIn("labels for owner/repo", str(cm.exception))

    def test_non_array_page_raises_fetch_error(self):
        with mock.patch.object(gh_client, "run_command", return_value=result('{"x": 1}')):
            with self.assertRaises(FetchError):
                self.client.api_paginated("repos/owner/repo/labels", "labels")


class GraphqlTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient("owner/repo")

    def test_graphql_passes_query_and_variables(self):
        payload = '{"data": {"repository": {"name": "repo"}}}'
        with mock.patch.object(
            gh_client, "run_command", return_value=result(payload)
        ) as run:
            data = self.client.graphql(
                "query($owner: String!) { repository(owner: $owner) { name } }",
                {"owner": "owner", "cursor": None},
            )
        self.assertEqual(data["data"]["repository"]["name"], "repo")
        args = run.call_args.args[0]
        self.assertIn("-F", args)
        self.assertIn("owner=owner", args)
        self.assertNotIn("cursor=None", args)

    def test_graphql_errors_raise_fetch_error(self):
        payload = '{"errors": [{"message": "denied"}]}'
        with mock.patch.object(gh_client, "run_command", return_value=result(payload)):
            with self.assertRaises(FetchError) as cm:
                self.client.graphql("query { viewer { login } }", {})
        self.assertIn("denied", str(cm.exception))


class PrViewTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient("owner/repo")

    def test_pr_view_requests_selected_fields(self):
        with mock.patch.object(
            gh_client, "run_command", return_value=result('{"number": 7}')
        ) as run:
            data = self.client.pr_view(7, ["number", "title"])
        self.assertEqual(data, {"number": 7})
        self.assertEqual(
            run.call_args.args[0],
            [
                "gh", "pr", "view", "7", "--repo", "owner/repo",
                "--json", "number,title",
            ],
        )

    def test_pr_view_failure_is_clear(self):
        with mock.patch.object(
            gh_client, "run_command", return_value=result(stderr="missing", code=1)
        ):
            with self.assertRaises(CommandError) as cm:
                self.client.pr_view(7, ["number"])
        self.assertIn("Could not view PR #7", str(cm.exception))


class PrChecksTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient("owner/repo")

    def test_successful_checks_output_is_returned(self):
        with mock.patch.object(
            gh_client, "run_command", return_value=result("unit\tpass\n")
        ):
            checks = self.client.pr_checks(7)
        self.assertEqual(checks, "unit\tpass")

    def test_failed_checks_command_raises_clear_error(self):
        with mock.patch.object(
            gh_client,
            "run_command",
            return_value=result(stderr="network unavailable", code=1),
        ):
            with self.assertRaises(CommandError) as cm:
                self.client.pr_checks(7)
        self.assertIn("Could not fetch checks for PR #7", str(cm.exception))
        self.assertIn("network unavailable", str(cm.exception))

    def test_empty_successful_checks_output_is_accepted(self):
        with mock.patch.object(gh_client, "run_command", return_value=result()):
            checks = self.client.pr_checks(7)
        self.assertEqual(checks, "No checks reported.")


if __name__ == "__main__":
    unittest.main()
