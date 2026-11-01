from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from utils.fs_utils import safe_copy_to_dir, validate_attachment


class AttachmentFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "permit.txt"
        self.source.write_text("original")
        self.destination = self.root / "attachments"

    def test_concurrent_copies_do_not_overwrite_each_other(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(lambda _: safe_copy_to_dir(self.source, self.destination), range(8)))
        self.assertEqual(len(set(paths)), 8)
        for path in paths:
            self.assertEqual(path.read_text(), "original")
        self.assertEqual(len(list(self.destination.iterdir())), 8)

    def test_partial_copy_is_removed(self):
        def fail_copy(source, destination):
            Path(destination).write_text("partial")
            raise OSError("Disk full")

        with patch("utils.fs_utils.shutil.copy2", side_effect=fail_copy):
            with self.assertRaises(OSError):
                safe_copy_to_dir(self.source, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_failed_rename_cleans_reservation(self):
        with patch("utils.fs_utils.os.replace", side_effect=OSError("Unavailable")):
            with self.assertRaises(OSError):
                safe_copy_to_dir(self.source, self.destination)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_overwrite_failure_keeps_old_file(self):
        old = safe_copy_to_dir(self.source, self.destination)
        with patch("utils.fs_utils.shutil.copy2", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                safe_copy_to_dir(self.source, self.destination, overwrite=True)
        self.assertEqual(old.read_text(), "original")

    def test_exact_limits_are_accepted(self):
        size = self.source.stat().st_size
        with patch("utils.fs_utils.settings.MAX_ATTACHMENT_SIZE", size):
            with patch("utils.fs_utils.settings.MAX_TOTAL_ATTACHMENTS_PER_RECORD", size * 2):
                validate_attachment(self.source, size)
                with self.assertRaises(ValueError):
                    validate_attachment(self.source, size + 1)
