import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QDate, QTimer
    from PySide6.QtTest import QTest
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
            self.wait_staging(dialog)
            dialog.deleteLater()
        self.workspace.cleanup()

    def wait_staging(self, dialog):
        deadline = time.monotonic() + 10
        while dialog._attachment_busy and time.monotonic() < deadline:
            QTest.qWait(5)
        self.assertFalse(dialog._attachment_busy, "Attachment staging worker did not finish")

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
        self.wait_staging(dialog)
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
        self.wait_staging(dialog)
        self.assertFalse(stage.exists())

    def test_attachment_duplicate_gets_new_name_without_overwrite(self):
        original = self.original()
        source = self.root / "old.txt"
        source.write_text("replacement")
        dialog = self.dialog({"id": "permit"})
        with patch.object(QFileDialog, "getOpenFileNames", return_value=([str(source)], "")):
            dialog.add_attachment()
        self.wait_staging(dialog)
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

    def test_service_attachment_factory_receives_record_and_readonly(self):
        from core.attachments import AttachmentPlan
        self.original()
        plan = AttachmentPlan(self.root / "attachments", "permit")
        service = Mock()
        service.attachment_plan.return_value = plan
        dialog = EngedelyDialog(
            engedely={"id": "permit"}, readonly=True, service=service,
            attachments_base=str(self.root / "attachments"))
        self.dialogs.append(dialog)
        service.attachment_plan.assert_called_once_with("permit", readonly=True)
        self.assertIs(dialog.attachment_plan, plan)

    def test_revoked_attachment_preview_shows_explicit_error(self):
        from core.auth import PermissionDenied
        self.original()
        dialog = self.dialog({"id": "permit"})
        dialog.attach_list.setCurrentRow(0)
        with patch.object(dialog.attachment_plan, "path_for",
                          side_effect=PermissionDenied("revoked")), \
                patch.object(QMessageBox, "warning") as warning:
            dialog.view_attachment()
        warning.assert_called_once()

    def test_explicit_attachment_plan_takes_precedence_over_factory(self):
        from core.attachments import AttachmentPlan
        plan = AttachmentPlan(self.root / "attachments")
        service = Mock()
        dialog = EngedelyDialog(
            attachment_plan=plan, service=service,
            attachments_base=str(self.root / "attachments"))
        self.dialogs.append(dialog)
        self.assertIs(dialog.attachment_plan, plan)
        service.attachment_plan.assert_not_called()

    def test_main_window_add_edit_dialog_payload_saves_through_service(self):
        from core.db import Database
        from core.services import PERMIT_FIELDS, PermitService
        from gui.main_window import MainWindow
        db = Database(self.root / "database.db", self.root / "backups")
        service = PermitService(db, attachments_root=self.root / "attachments")
        service.bootstrap_admin("admin", "dialog-integration-password")
        service.save_site({"nev": "Site", "ktj": "123", "cim": "Address"})
        window = MainWindow(db, {"attachments": self.root / "attachments"}, None, service)
        try:
            dialog = window._permit_dialog()
            self.assertIsNotNone(dialog)
            self.dialogs.append(dialog)
            dialog.hatarozatszam.setText("ABC")
            dialog.modosito_input.setText("Modification")
            dialog._on_modosito_add()
            values = dialog.get_data()
            self.assertLessEqual(set(values), PERMIT_FIELDS)
            self.assertEqual(values["modosito_hat"], "Modification")
            dialog._on_accept()
            saved = service.save_permit(values, attachment_plan=dialog.attachment_plan)
            self.assertIsNone(saved["engedely_nev"])
            dialog.cleanup_attachments()
            edited = window._permit_dialog(saved)
            self.assertIsNotNone(edited)
            self.dialogs.append(edited)
            edited.targy.setText("Edited")
            update = edited.get_data()
            self.assertLessEqual(set(update), PERMIT_FIELDS)
            update["id"] = saved["id"]
            edited._on_accept()
            result = service.save_permit(
                update, expected_version=saved["version"], attachment_plan=edited.attachment_plan)
            self.assertEqual(result["engedely_nev"], "Edited")
            edited.cleanup_attachments()
        finally:
            window.close()
            window.deleteLater()

    def test_attachment_staging_runs_off_gui_thread_and_blocks_save_until_finished(self):
        source = self.root / "large.txt"
        source.write_text("data")
        dialog = self.dialog()
        release, started = threading.Event(), threading.Event()
        threads, responsive = [], []
        original_add = dialog.attachment_plan.add

        def delayed_add(*args, **kwargs):
            threads.append(threading.get_ident())
            started.set()
            release.wait(5)
            return original_add(*args, **kwargs)

        with patch.object(dialog.attachment_plan, "add", side_effect=delayed_add), \
                patch.object(QFileDialog, "getOpenFileNames", return_value=([str(source)], "")):
            dialog.add_attachment()
            self.assertTrue(started.wait(2))
            self.assertTrue(dialog._attachment_busy)
            self.assertFalse(dialog.save_btn.isEnabled())
            self.assertFalse(dialog.cancel_btn.isEnabled())
            dialog._on_accept()
            self.assertNotEqual(dialog.result(), QDialog.Accepted)
            QTimer.singleShot(0, lambda: responsive.append(True))
            QApplication.processEvents()
            self.assertTrue(responsive)
            release.set()
            self.wait_staging(dialog)
        self.assertNotEqual(threads, [threading.get_ident()])
        self.assertTrue(dialog.save_btn.isEnabled())
        self.assertEqual(dialog.attach_list.item(0).text(), "large.txt")

    def test_cancel_during_staging_waits_then_cleans_without_touching_originals(self):
        original = self.original()
        source = self.root / "new.txt"
        source.write_text("data")
        dialog = self.dialog({"id": "permit"})
        release, started = threading.Event(), threading.Event()
        original_add = dialog.attachment_plan.add

        def delayed_add(*args, **kwargs):
            started.set()
            release.wait(5)
            return original_add(*args, **kwargs)

        with patch.object(dialog.attachment_plan, "add", side_effect=delayed_add), \
                patch.object(QFileDialog, "getOpenFileNames", return_value=([str(source)], "")):
            dialog.add_attachment()
            self.assertTrue(started.wait(2))
            dialog.reject()
            self.assertTrue(dialog._attachment_busy)
            release.set()
            self.wait_staging(dialog)
        self.assertFalse(dialog.attachment_plan._stage.exists())
        self.assertEqual((original / "old.txt").read_text(), "original")
        self.assertFalse((original / "new.txt").exists())

    def test_destroyed_owner_cancels_worker_and_cleans_staging(self):
        from shiboken6 import delete
        source = self.root / "new.txt"
        source.write_text("data")
        dialog = self.dialog()
        plan = dialog.attachment_plan
        release, started = threading.Event(), threading.Event()
        original_add = plan.add

        def delayed_add(*args, **kwargs):
            started.set()
            release.wait(5)
            return original_add(*args, **kwargs)

        with patch.object(plan, "add", side_effect=delayed_add), \
                patch.object(QFileDialog, "getOpenFileNames", return_value=([str(source)], "")):
            dialog.add_attachment()
            self.assertTrue(started.wait(2))
            self.dialogs.remove(dialog)
            delete(dialog)
            release.set()
            deadline = time.monotonic() + 10
            while plan._stage.exists() and time.monotonic() < deadline:
                QTest.qWait(5)
        self.assertFalse(plan._stage.exists())
        QTest.qWait(10)

    def test_calendar_underflow_displays_context_and_blocks_accept(self):
        dialog = self.dialog()
        dialog.felulvizsgalatok = [{"type": "before_expiry", "days": 1000000, "note": "old"}]
        dialog.hatary_date.setDate(QDate(1752, 1, 1))
        self.assertIn("naptári tartomány", dialog.next_review_lbl.text())
        with patch.object(QMessageBox, "warning") as warning:
            dialog._on_accept()
        self.assertIn("1. felülvizsgálati bejegyzés", warning.call_args.args[2])
        self.assertNotEqual(dialog.result(), QDialog.Accepted)


if __name__ == "__main__":
    unittest.main()
