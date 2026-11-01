import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog

from core.document_ocr import ExtractionRule, OcrRules
from gui.ocr_settings_dialog import OcrSettingsDialog, load_ocr_rules


class OcrSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "settings.ini")
        self.settings = QSettings(self.path, QSettings.IniFormat)
        self.dialog = OcrSettingsDialog(settings=self.settings)
        self.addCleanup(self.dialog.deleteLater)

    def test_save_persists_rules_and_preview_uses_them(self):
        rules = OcrRules(ExtractionRule("Kezdet", "Vége"), ExtractionRule("Lejárat"))
        self.dialog.set_rules(rules)
        self.dialog.sample.setPlainText("Kezdet próba Vége\nLejárat: 2030.12.31.")
        self.dialog.preview()
        self.assertIn("Tárgy: próba", self.dialog.preview_result.toPlainText())
        self.assertIn("2030-12-31", self.dialog.preview_result.toPlainText())
        self.dialog.save()
        self.assertEqual(self.dialog.result(), QDialog.Accepted)
        self.assertEqual(load_ocr_rules(QSettings(self.path, QSettings.IniFormat)), rules)

    def test_cancel_does_not_change_settings(self):
        self.dialog.set_rules(OcrRules(subject=ExtractionRule("Kezdet")))
        self.dialog.reject()
        self.assertEqual(load_ocr_rules(self.settings), OcrRules())

    def test_invalid_saved_rules_report_error(self):
        for raw in ("not json", "[]", '{"subject": {}, "validity": {"end": "stop"}}'):
            self.settings.setValue("ocr/rules", raw)
            with self.assertRaises(ValueError):
                load_ocr_rules(self.settings)

    def test_end_without_start_cannot_be_saved(self):
        self.dialog.inputs["subject"][1].setText("Vége")
        with patch("gui.ocr_settings_dialog.QMessageBox.warning") as warning:
            self.dialog.save()
        warning.assert_called_once()
        self.assertFalse(self.settings.contains("ocr/rules"))
