"""CLI parsing and selection tests. No network, no gh, no git."""

import contextlib
import io
import unittest

from odytor import __version__
from odytor.cli import (
    build_parser,
    main,
    selected_action,
    selected_target,
    validate_selection,
)


def parse(argv):
    return build_parser().parse_args(argv)


class HelpVersionTests(unittest.TestCase):
    def test_help_exits_cleanly(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
            parse(["--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_version_prints_version(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(out):
            parse(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(__version__, out.getvalue())


class ActionSelectionTests(unittest.TestCase):
    def test_missing_action_errors_clearly(self):
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
            parse([])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("required", err.getvalue())

    def test_print_pr_parses_as_print_pr_3128(self):
        args = parse(["--print", "--pr", "3128"])
        self.assertEqual(selected_action(args), "print")
        target = selected_target(args)
        self.assertEqual((target.kind, target.number), ("pr", 3128))

    def test_review_pr_parses_as_review_pr_3128(self):
        args = parse(["--review", "--pr", "3128"])
        self.assertEqual(selected_action(args), "review")
        target = selected_target(args)
        self.assertEqual((target.kind, target.number), ("pr", 3128))

    def test_labels_action_has_no_target(self):
        args = parse(["--labels"])
        self.assertEqual(selected_action(args), "labels")
        self.assertIsNone(selected_target(args))

    def test_deferred_flags_are_rejected_clearly(self):
        for flag in ("--followups", "--stale", "--milestone"):
            with self.subTest(flag=flag):
                err = io.StringIO()
                with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
                    main([flag])
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("deferred", err.getvalue())

    def test_guarded_subcommand_is_not_exposed(self):
        for command in ("audit", "approve", "approve-merge"):
            with self.subTest(command=command):
                err = io.StringIO()
                with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
                    main([command, "--help"])
                self.assertEqual(cm.exception.code, 2)
                self.assertIn("not included", err.getvalue())

    def test_help_describes_review_and_comment_window_scope(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(out):
            parse(["--help"])
        help_text = out.getvalue()
        self.assertIn("local review-prep summary, not a verdict", help_text)
        self.assertIn("Limit conversation comments", help_text)
        self.assertIn("Conversation comment order", help_text)


class ValidateSelectionTests(unittest.TestCase):
    def _error(self, argv):
        parser = build_parser()
        args = parser.parse_args(argv)
        action = selected_action(args)
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(err):
            validate_selection(parser, action, args, argv)
        return cm.exception, err.getvalue()

    def test_missing_target_for_print_errors(self):
        exc, _ = self._error(["--print"])
        self.assertEqual(exc.code, 2)

    def test_missing_target_for_review_errors(self):
        exc, _ = self._error(["--review"])
        self.assertEqual(exc.code, 2)

    def test_action_without_target_takes_no_target(self):
        # --labels with a target is an invalid combination.
        exc, _ = self._error(["--labels", "--pr", "3128"])
        self.assertEqual(exc.code, 2)

    def test_valid_print_selection_returns_target(self):
        parser = build_parser()
        argv = ["--print", "--issue", "2523", "--comments", "10", "--order", "latest"]
        args = parser.parse_args(argv)
        target = validate_selection(parser, selected_action(args), args, argv)
        self.assertEqual((target.kind, target.number), ("issue", 2523))

    def test_comments_are_rejected_outside_print(self):
        # Both split (--comments 10) and equals (--comments=10) forms must fail.
        for argv in (
            ["--review", "--pr", "3128", "--comments", "10"],
            ["--review", "--pr", "3128", "--comments=10"],
            ["--labels", "--comments", "10"],
            ["--labels", "--comments=10"],
        ):
            with self.subTest(argv=argv):
                exc, error = self._error(argv)
                self.assertEqual(exc.code, 2)
                self.assertIn("--comments is only valid with --print", error)

    def test_explicit_order_is_rejected_outside_print(self):
        # Both split (--order latest) and equals (--order=latest) forms must fail.
        for argv in (
            ["--review", "--pr", "3128", "--order", "latest"],
            ["--review", "--pr", "3128", "--order=latest"],
            ["--labels", "--order", "latest"],
            ["--labels", "--order=latest"],
        ):
            with self.subTest(argv=argv):
                exc, error = self._error(argv)
                self.assertEqual(exc.code, 2)
                self.assertIn("--order is only valid with --print", error)

    def test_print_accepts_equals_form_comments_and_order(self):
        parser = build_parser()
        argv = ["--print", "--pr", "3128", "--comments=10", "--order=latest"]
        args = parser.parse_args(argv)
        target = validate_selection(parser, selected_action(args), args, argv)
        self.assertEqual((target.kind, target.number), ("pr", 3128))
        self.assertEqual(args.comments, 10)
        self.assertEqual(args.order, "latest")

    def test_default_order_does_not_break_non_print_actions(self):
        for argv in (["--review", "--pr", "3128"], ["--labels"]):
            with self.subTest(argv=argv):
                parser = build_parser()
                args = parser.parse_args(argv)
                validate_selection(parser, selected_action(args), args, argv)


class InvalidCombinationTests(unittest.TestCase):
    def _exit_code(self, argv):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            parse(argv)
        return cm.exception.code

    def test_two_actions_are_mutually_exclusive(self):
        self.assertEqual(self._exit_code(["--print", "--review", "--pr", "3128"]), 2)

    def test_two_targets_are_mutually_exclusive(self):
        self.assertEqual(self._exit_code(["--print", "--pr", "3128", "--issue", "5"]), 2)

    def test_non_positive_target_rejected(self):
        self.assertEqual(self._exit_code(["--print", "--pr", "0"]), 2)

    def test_discussion_uses_positive_number_validation(self):
        # --discussion must share the same positive-number parser as --pr/--issue.
        for value in ("0", "-1", "abc"):
            with self.subTest(value=value):
                self.assertEqual(self._exit_code(["--print", "--discussion", value]), 2)

    def test_positive_discussion_number_parses(self):
        args = parse(["--print", "--discussion", "2528"])
        target = selected_target(args)
        self.assertEqual((target.kind, target.number), ("discussion", 2528))

    def test_option_like_repo_value_reaches_repo_validation(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["--labels", "--repo", "-/-"])
        self.assertEqual(code, 1)
        self.assertIn("Invalid repository '-/-'", err.getvalue())


if __name__ == "__main__":
    unittest.main()
