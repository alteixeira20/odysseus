"""Repository detection tests. git is mocked; nothing runs live."""

import unittest
from unittest import mock

from odytor import git_repo
from odytor.errors import RepoDetectionError
from odytor.models import CommandResult


class RemoteParsingTests(unittest.TestCase):
    def test_ssh_remote_parses(self):
        self.assertEqual(
            git_repo.parse_github_remote("git@github.com:owner/repo.git"),
            "owner/repo",
        )

    def test_https_remote_with_git_suffix_parses(self):
        self.assertEqual(
            git_repo.parse_github_remote("https://github.com/owner/repo.git"),
            "owner/repo",
        )

    def test_https_remote_without_suffix_parses(self):
        self.assertEqual(
            git_repo.parse_github_remote("https://github.com/owner/repo"),
            "owner/repo",
        )

    def test_non_github_remote_returns_none(self):
        self.assertIsNone(git_repo.parse_github_remote("https://gitlab.com/owner/repo.git"))
        self.assertIsNone(git_repo.parse_github_remote("git@bitbucket.org:owner/repo.git"))


class ValidateRepoTests(unittest.TestCase):
    def test_valid_repos_pass_through(self):
        for value in (
            "owner/name",
            "owner-name/repo_name",
            "owner.name/.github",
            "Owner123/repo.v2",
        ):
            with self.subTest(value=value):
                self.assertEqual(git_repo.validate_repo(value), value)

    def test_short_single_character_names_are_valid(self):
        # Short owner/name components are not inherently invalid.
        for value in ("a/b", "x/y"):
            with self.subTest(value=value):
                self.assertEqual(git_repo.validate_repo(value), value)

    def test_invalid_repos_raise(self):
        for value in (
            "invalid-repo-name",
            "owner/..",
            "-/-",
            "owner/name:bad",
            "owner/",
            "/repo",
            "owner/repo/extra",
            "owner name/repo",
            "owner/re po",
        ):
            with self.subTest(value=value), self.assertRaises(RepoDetectionError):
                git_repo.validate_repo(value)


class DetectionFlowTests(unittest.TestCase):
    def test_not_inside_git_gives_clear_error(self):
        with mock.patch.object(git_repo, "require_command"), mock.patch.object(
            git_repo, "run_command", return_value=CommandResult("", "", 128)
        ):
            with self.assertRaises(RepoDetectionError) as cm:
                git_repo.resolve_repo(None)
        self.assertIn("not inside a git repository", str(cm.exception))

    def test_explicit_repo_bypasses_detection(self):
        with mock.patch.object(
            git_repo, "detect_repo", side_effect=AssertionError("should not auto-detect")
        ):
            self.assertEqual(git_repo.resolve_repo("owner/name"), "owner/name")


if __name__ == "__main__":
    unittest.main()
