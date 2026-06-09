"""Progress/status tests and CLI-level wiring (stderr vs stdout, --quiet, --save)."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odytor import cli, commands
from odytor.progress import PREFIX, Progress, disabled


def _fake_print(client, target, window, progress):
    progress.step("Fetching comments...")
    return "REPORT-BODY\n", "pr-1"


class ProgressUnitTests(unittest.TestCase):
    def test_step_writes_prefixed_line(self):
        buf = io.StringIO()
        Progress(enabled=True, stream=buf).step("Fetching comments...")
        self.assertEqual(buf.getvalue(), f"{PREFIX} Fetching comments...\n")

    def test_item_shows_total_and_percent(self):
        buf = io.StringIO()
        Progress(enabled=True, stream=buf).item(7, 42, "Fetching replies for comment")
        self.assertEqual(buf.getvalue().strip(), f"{PREFIX} Fetching replies for comment 7/42 (17%)")

    def test_item_without_total_omits_percent(self):
        buf = io.StringIO()
        Progress(enabled=True, stream=buf).item(3, None, "Scanning")
        self.assertEqual(buf.getvalue().strip(), f"{PREFIX} Scanning 3")

    def test_page_format(self):
        buf = io.StringIO()
        Progress(enabled=True, stream=buf).page("Fetching discussion comments", 2)
        self.assertEqual(buf.getvalue().strip(), f"{PREFIX} Fetching discussion comments page 2...")

    def test_disabled_is_silent(self):
        buf = io.StringIO()
        p = Progress(enabled=False, stream=buf)
        p.step("x")
        p.item(1, 2, "y")
        p.done()
        self.assertEqual(buf.getvalue(), "")

    def test_disabled_helper(self):
        self.assertFalse(disabled().enabled)

    def test_default_stream_is_stderr(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            Progress(enabled=True).step("hello")
        self.assertIn(f"{PREFIX} hello", captured.getvalue())


class MainWiringTests(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(commands, "make_client", return_value=mock.Mock()), \
                mock.patch.object(commands, "run_print", side_effect=_fake_print), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_progress_goes_to_stderr_not_stdout(self):
        code, out, err = self._run(["--print", "--pr", "1"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "REPORT-BODY\n")
        self.assertNotIn(PREFIX, out)
        self.assertIn(f"{PREFIX} Fetching comments...", err)
        self.assertIn(f"{PREFIX} Done.", err)

    def test_quiet_suppresses_progress(self):
        code, out, err = self._run(["--print", "--pr", "1", "--quiet"])
        self.assertEqual(out, "REPORT-BODY\n")
        self.assertNotIn(PREFIX, err)

    def test_quiet_save_suppresses_save_location(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        code, out, err = self._run(
            [
                "--print", "--pr", "1", "--save",
                "--output-dir", tmp.name, "--quiet",
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(out, "REPORT-BODY\n")
        self.assertEqual(err, "")
        self.assertEqual(len(list(Path(tmp.name).glob("odytor-pr-1-*.txt"))), 1)

    def test_saved_file_excludes_progress(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        code, out, err = self._run(
            ["--print", "--pr", "1", "--save", "--output-dir", tmp.name]
        )
        files = list(Path(tmp.name).glob("odytor-pr-1-*.txt"))
        self.assertEqual(len(files), 1)
        saved = files[0].read_text(encoding="utf-8")
        self.assertEqual(saved, "REPORT-BODY\n")
        self.assertNotIn(PREFIX, saved)
        self.assertIn("Saved to:", err)

    def test_file_output_dir_fails_before_fetching_or_printing(self):
        with tempfile.NamedTemporaryFile() as output_file:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(commands, "make_client") as make_client, \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                    self.assertRaises(SystemExit) as cm:
                cli.main(
                    [
                        "--review", "--pr", "3128", "--save",
                        "--output-dir", output_file.name,
                    ]
                )

        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("--output-dir is not a directory", err.getvalue())
        make_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
