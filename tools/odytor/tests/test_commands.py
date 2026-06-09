"""Client construction failure tests. No external commands run."""

import unittest
from unittest import mock

from odytor import commands
from odytor.errors import CommandError, RepoDetectionError


class MakeClientFailureTests(unittest.TestCase):
    def test_invalid_explicit_repo_stops_before_gh_checks(self):
        with mock.patch.object(commands, "require_command") as require, \
                mock.patch.object(commands, "require_gh_auth") as auth:
            with self.assertRaises(RepoDetectionError) as cm:
                commands.make_client("owner/..")

        self.assertIn("Invalid repository", str(cm.exception))
        require.assert_not_called()
        auth.assert_not_called()

    def test_missing_gh_stops_before_auth_or_repo_detection(self):
        missing = CommandError("required command 'gh' not found on PATH.")
        with mock.patch.object(
            commands, "require_command", side_effect=missing
        ), mock.patch.object(commands, "require_gh_auth") as auth, mock.patch.object(
            commands, "resolve_repo"
        ) as resolve:
            with self.assertRaises(CommandError) as cm:
                commands.make_client(None)

        self.assertIn("required command 'gh' not found", str(cm.exception))
        auth.assert_not_called()
        resolve.assert_not_called()

    def test_missing_git_from_repo_detection_is_reported(self):
        missing = CommandError("required command 'git' not found on PATH.")
        with mock.patch.object(commands, "require_command"), mock.patch.object(
            commands, "require_gh_auth"
        ), mock.patch.object(commands, "resolve_repo", side_effect=missing):
            with self.assertRaises(CommandError) as cm:
                commands.make_client(None)

        self.assertIn("required command 'git' not found", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
