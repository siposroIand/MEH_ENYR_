import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QDate
    from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox
    from gui.engedely_dialog import EngedelyDialog, ReviewEntryDialog
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "PySide6 is unavailable")
class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix=".dialog-test-", dir=Path.cwd())
        self.root = Path(self.workspace.name)
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.cleanup_attachments()
            dialog.deleteLater()
        self.workspace.cleanup()

    def dialog(self, entity=None):
        dialog = EngedelyDialog(
            engedely=entity, telephelyek=[{"id": "site", "nev": "Site"}],
            attachments_base=str(self.root / "attachments"))
        dialog.hatarozatszam.setText("ABC")
        self.dialogs.append(dialog)
        return dialog

    def original(self):
        original = self.root / "attachments" / "permit"
        original.mkdir(parents=True)
        (original / "old.txt").write_text("original")
        return original

    def test_explicit_empty_does_not_resurrect_legacy(self):
        dialog = self.dialog({"id": "permit", "felulvizsgalatok": "[]",
                              "felulvizsgalat_date": "2030-01-01"})
        self.assertEqual(dialog.felulvizsgalatok, [])
        self.assertEqual(dialog.get_data()["felulvizsgalatok"], [])
        self.assertIsNone(dialog.get_data()["felulvizsgalat_date"])

    def test_malformed_data_preserved_and_save_edit_blocked(self):
        raw = '[{"type":"date","date":"not-a-date"}]'
        dialog = self.dialog({"id": "permit", "felulvizsgalatok": raw})
        self.assertEqual(dialog.get_data()["felulvizsgalatok"], raw)
        with patch.object(QMessageBox, "warning") as warning:
            dialog._on_accept()
            dialog.add_review_entry()
            self.assertEqual(warning.call_count, 2)
        self.assertNotEqual(dialog.result(), QDialog.Accepted)

    def test_expiry_changes_recompute_and_nonfixed_disables_date(self):
        dialog = self.dialog({"id": "permit", "hataly_tipus": "fixed",
                              "lejarat": "2090-01-10", "felulvizsgalatok": [
                                  {"type": "before_expiry", "days": 0}]})
        self.assertIn("2090-01-10", dialog.next_review_lbl.text())
        dialog.hatary_date.setDate(QDate(2090, 2, 10))
        self.assertIn("2090-02-10", dialog.next_review_lbl.text())
        dialog.rb_indef.setChecked(True)
        self.assertFalse(dialog.hatary_date.isEnabled())
        self.assertNotIn("2090-02-10", dialog.next_review_lbl.text())

    def test_attachment_removal_is_staged_and_reject_discards(self):
        original = self.original()
        dialog = self.dialog({"id": "permit"})
        dialog.attach_list.setCurrentRow(0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            dialog.remove_attachment()
        self.assertTrue((original / "old.txt").exists())
        self.assertEqual(dialog.attach_list.count(), 0)
        dialog.reject()
        self.assertTrue((original / "old.txt").exists())
        self.assertFalse(dialog.attachment_plan._stage.exists())

    def test_attachment_add_accept_does_not_commit(self):
        original = self.original()
        source = self.root / "new.txt"
        source.write_text("new")
        dialog = self.dialog({"id": "permit"})
        with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(source)], "")):
            dialog.add_attachment()
        stage = dialog.attachment_plan._stage
        self.assertEqual(dialog.attachment_plan.path_for("new.txt").read_text(), "new")
        self.assertFalse((original / "new.txt").exists())
        dialog._on_accept()
        self.assertEqual(dialog.result(), QDialog.Accepted)
        self.assertTrue(stage.exists())
        self.assertFalse((original / "new.txt").exists())
        dialog.cleanup_attachments()
        self.assertFalse(stage.exists())

    def test_close_cleans_staged_files(self):
        source = self.root / "new.txt"
        source.write_text("new")
        dialog = self.dialog()
        with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(source)], "")):
            dialog.add_attachment()
        stage = dialog.attachment_plan._stage
        dialog.close()
        self.assertFalse(stage.exists())

    def test_attachment_duplicate_gets_new_name_without_overwrite(self):
        original = self.original()
        source = self.root / "old.txt"
        source.write_text("replacement")
        dialog = self.dialog({"id": "permit"})
        with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(source)], "")):
            dialog.add_attachment()
        self.assertEqual(dialog.attachment_plan.list_names(), ["old.txt", "old_1.txt"])
        self.assertEqual((original / "old.txt").read_text(), "original")
        dialog.reject()
        self.assertFalse((original / "old_1.txt").exists())

    def test_review_type_switch_revalidates_duplicate(self):
        editor = ReviewEntryDialog(disallow_serials={
            "date|" + QDate.currentDate().toString("yyyy-MM-dd")}, min_date_iso="2090-01-01")
        self.assertFalse(editor._ok_btn.isEnabled())
        editor.rb_before.setChecked(True)
        self.assertTrue(editor._ok_btn.isEnabled())
        self.assertEqual(editor.get_value()["days"], 0)
        editor.reject()

    def test_existing_large_day_offset_is_not_clipped(self):
        editor = ReviewEntryDialog(entry={"type": "before_expiry", "days": 5000},
                                   min_date_iso="2090-01-01")
        self.assertEqual(editor.get_value()["days"], 5000)
        editor.reject()

    def test_new_dialog_defaults_preserve_filter_selection(self):
        from config.app_config import get_ordered_categories
        categories = get_ordered_categories()
        dialog = EngedelyDialog(
            telephelyek=[{"id": "first", "nev": "First"}, {"id": "chosen", "nev": "Chosen"}],
            attachments_base=str(self.root / "attachments"),
            defaults={"site": "chosen", "category": categories[-1]},
        )
        self.dialogs.append(dialog)
        self.assertEqual(dialog.telephely_combo.currentData(), "chosen")
        self.assertEqual(dialog.kategoria.currentText(), categories[-1])

    def test_readonly_details_keep_attachment_preview_without_edit_controls(self):
        self.original()
        dialog = EngedelyDialog(
            engedely={"id": "permit"}, readonly=True,
            attachments_base=str(self.root / "attachments"))
        self.dialogs.append(dialog)
        self.assertFalse(dialog.save_btn.isEnabled())
        self.assertFalse(dialog.targy.isEnabled())
        self.assertFalse(dialog.attach_add.isEnabled())
        self.assertFalse(dialog.attach_del.isEnabled())
        self.assertTrue(dialog.attach_view.isEnabled())
        self.assertTrue(dialog.attach_list.isEnabled())
        self.assertEqual(dialog.attach_list.count(), 1)
        with patch.object(QFileDialog, "getOpenFileNames") as choose:
            dialog.add_attachment()
            choose.assert_not_called()
        with patch.object(QMessageBox, "warning"):
            dialog._on_accept()
        self.assertNotEqual(dialog.result(), QDialog.Accepted)


if __name__ == "__main__":
    unittest.main()
