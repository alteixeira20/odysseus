"""save_output tests: filename shape and custom directory handling."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from odytor import output
from odytor.output import save_output

NAME_RE = re.compile(r"^odytor-(.+)-\d{8}-\d{6}-[0-9a-f]{12}\.txt$")


class SaveOutputTests(unittest.TestCase):
    def _fixed_timestamp(self):
        patched = mock.patch.object(output, "datetime")
        datetime = patched.start()
        self.addCleanup(patched.stop)
        datetime.now.return_value.strftime.return_value = "20260609-120000"

    def _save(self, slug, content="body"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        return directory, save_output(directory, slug, content)

    def test_filename_uses_slug_and_timestamp(self):
        for slug in ("pr-3128", "issue-2523", "discussion-2528"):
            with self.subTest(slug=slug):
                _, path = self._save(slug)
                match = NAME_RE.match(path.name)
                self.assertIsNotNone(match, f"unexpected filename: {path.name}")
                self.assertEqual(match.group(1), slug)

    def test_save_respects_custom_output_dir(self):
        directory, path = self._save("pr-3128", content="hello")
        self.assertEqual(path.parent, directory)
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), "hello")

    def test_save_creates_missing_directory(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        nested = Path(tmp.name) / "exports" / "deep"
        path = save_output(nested, "labels", "data")
        self.assertTrue(path.exists())
        self.assertEqual(path.parent, nested)

    def test_two_saves_in_same_second_use_distinct_files(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        self._fixed_timestamp()

        with mock.patch.object(
            output.secrets, "token_hex", side_effect=("a" * 12, "b" * 12)
        ):
            first = save_output(directory, "labels", "first")
            second = save_output(directory, "labels", "second")

        self.assertNotEqual(first, second)
        self.assertEqual(first.read_text(encoding="utf-8"), "first")
        self.assertEqual(second.read_text(encoding="utf-8"), "second")

    def test_preexisting_collision_is_not_overwritten(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        self._fixed_timestamp()

        with mock.patch.object(output.secrets, "token_hex", return_value="a" * 12):
            first = save_output(directory, "labels", "original")
        with mock.patch.object(
            output.secrets, "token_hex", side_effect=("a" * 12, "b" * 12)
        ):
            second = save_output(directory, "labels", "new")

        self.assertEqual(first.read_text(encoding="utf-8"), "original")
        self.assertEqual(second.read_text(encoding="utf-8"), "new")

    def test_symlink_collision_is_not_followed(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        target = directory / "target.txt"
        target.write_text("unchanged", encoding="utf-8")
        self._fixed_timestamp()

        with mock.patch.object(output.secrets, "token_hex", return_value="a" * 12):
            initial = save_output(directory, "labels", "initial")
        initial.unlink()
        try:
            initial.symlink_to(target)
        except OSError as error:
            self.skipTest(f"symlinks unavailable: {error}")

        with mock.patch.object(
            output.secrets, "token_hex", side_effect=("a" * 12, "b" * 12)
        ):
            saved = save_output(directory, "labels", "report")

        self.assertTrue(initial.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(saved.read_text(encoding="utf-8"), "report")


if __name__ == "__main__":
    unittest.main()
