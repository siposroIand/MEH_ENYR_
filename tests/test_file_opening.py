import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QDialog, QLabel

from gui.engedely_dialog import EngedelyDialog
from gui.file_opener import open_local_path


class FileOpeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_local_url_preserves_special_characters(self):
        path = self.root / "Engedély #1 (másolat).pdf"
        path.write_bytes(b"fixture")
        with patch("gui.file_opener.QDesktopServices.openUrl", return_value=True) as open_url:
            open_local_path(path)
        self.assertEqual(open_url.call_args.args[0].toLocalFile(), str(path))
        self.assertTrue(open_url.call_args.args[0].isLocalFile())

    def test_missing_path_and_os_refusal_are_errors(self):
        with patch("gui.file_opener.QDesktopServices.openUrl", return_value=False) as open_url:
            with self.assertRaises(FileNotFoundError):
                open_local_path(self.root / "missing.pdf")
            open_url.assert_not_called()
            with self.assertRaises(OSError):
                open_local_path(self.root)

    def make_dialog(self, filename):
        folder = self.root / "permit"
        folder.mkdir()
        path = folder / filename
        path.write_bytes(b"fixture")
        dialog = EngedelyDialog(engedely={"id": "permit"}, attachments_base=str(self.root))
        self.addCleanup(dialog.deleteLater)
        return dialog, path

    def test_single_attachment_opens_without_explicit_selection(self):
        dialog, path = self.make_dialog("permit.pdf")
        with patch("gui.engedely_dialog.open_local_path") as opener:
            dialog.view_attachment()
        opener.assert_called_once_with(path)

    def test_open_failure_is_visible(self):
        dialog, _ = self.make_dialog("permit.pdf")
        with patch("gui.engedely_dialog.open_local_path", side_effect=OSError("No association")):
            with patch("gui.engedely_dialog.QMessageBox.warning") as warning:
                dialog.view_attachment()
        self.assertIn("No association", warning.call_args.args[2])

    def test_image_preview_and_double_click(self):
        dialog, path = self.make_dialog("preview.png")
        image = QImage(80, 40, QImage.Format_RGB32)
        image.fill(Qt.white)
        self.assertTrue(image.save(str(path)))
        with patch.object(QDialog, "exec", return_value=QDialog.Accepted) as execute:
            with patch("gui.engedely_dialog.QMessageBox.warning") as warning:
                dialog.attach_list.setCurrentRow(0)
                dialog.attach_list.itemDoubleClicked.emit(dialog.attach_list.item(0))
        execute.assert_called_once()
        warning.assert_not_called()
        preview = dialog.findChildren(QDialog)[-1]
        self.assertTrue(any(not label.pixmap().isNull() for label in preview.findChildren(QLabel)))
