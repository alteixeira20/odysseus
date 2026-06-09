"""Dependency and authentication error tests. No external commands run."""

import unittest
from unittest import mock

from odytor import utils
from odytor.errors import CommandError
from odytor.models import CommandResult


class DependencyErrorTests(unittest.TestCase):
    def test_missing_gh_has_official_install_link(self):
        with mock.patch.object(utils.shutil, "which", return_value=None):
            with self.assertRaises(CommandError) as cm:
                utils.require_command("gh")
        self.assertIn("required command 'gh' not found", str(cm.exception))
        self.assertIn("https://cli.github.com/", str(cm.exception))

    def test_missing_git_has_clear_message(self):
        with mock.patch.object(utils.shutil, "which", return_value=None):
            with self.assertRaises(CommandError) as cm:
                utils.require_command("git")
        self.assertIn("required command 'git' not found", str(cm.exception))


class AuthenticationErrorTests(unittest.TestCase):
    def test_unauthenticated_gh_suggests_login(self):
        result = CommandResult("", "not logged into any GitHub hosts", 1)
        with mock.patch.object(utils, "run_command", return_value=result):
            with self.assertRaises(CommandError) as cm:
                utils.require_gh_auth()
        message = str(cm.exception)
        self.assertIn("not authenticated", message)
        self.assertIn("gh auth login", message)
        self.assertIn("not logged into any GitHub hosts", message)

    def test_authenticated_gh_passes(self):
        result = CommandResult("", "", 0)
        with mock.patch.object(utils, "run_command", return_value=result):
            utils.require_gh_auth()


class RunCommandTests(unittest.TestCase):
    def test_subprocess_output_is_decoded_as_utf8(self):
        completed = mock.Mock(stdout="café", stderr="", returncode=0)
        with mock.patch.object(
            utils.subprocess, "run", return_value=completed
        ) as run:
            result = utils.run_command(["gh", "api", "repos/owner/repo"])

        self.assertEqual(result.stdout, "café")
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")


if __name__ == "__main__":
    unittest.main()
