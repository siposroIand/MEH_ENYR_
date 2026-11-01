from datetime import date
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QTabWidget, QTextEdit

from core.document_ocr import DocumentFields
from gui.engedely_dialog import EngedelyDialog


class PermitDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dialog = EngedelyDialog(
            telephelyek=[{"id": "site", "nev": "Telephely"}],
            attachments_base=str(self.root / "attachments"),
        )
        self.dialog.hatarozatszam.setText("123/2026")
        self.addCleanup(self.dialog.deleteLater)

    def wait_for_worker(self):
        deadline = time.monotonic() + 10
        while self.dialog._ocr_worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.dialog._ocr_worker)

    def test_attachment_browser_is_on_data_entry_tab(self):
        tabs = self.dialog.findChild(QTabWidget)
        self.assertTrue(tabs.widget(0).isAncestorOf(self.dialog.attach_add))
        self.assertNotIn("Csatolmányok", [tabs.tabText(i) for i in range(tabs.count())])

    def test_dialog_fits_smaller_screen(self):
        self.assertLessEqual(self.dialog.minimumSizeHint().height(), 720)

    def test_amending_decisions_visible_on_general_tab_and_files_collapsed(self):
        tabs = self.dialog.findChild(QTabWidget)
        self.assertTrue(tabs.widget(0).isAncestorOf(self.dialog.modosito_list))
        self.assertTrue(self.dialog.attach_list.isHidden())
        self.dialog.attach_toggle.setChecked(True)
        self.assertFalse(self.dialog.attach_list.isHidden())
        self.dialog.modosito_input.setText("M/2026")
        self.dialog._on_modosito_add()
        self.assertEqual(self.dialog.get_data()["modosito_hat"], "M/2026")

    def test_attachment_size_rechecked_at_commit_after_another_users_upload(self):
        from config import settings
        self.dialog._staging_dir.mkdir(parents=True)
        (self.dialog._staging_dir / "new.txt").write_bytes(b"new")
        destination = self.root / "attachments" / "saved"
        destination.mkdir(parents=True)
        (destination / "other.txt").write_bytes(b"other")
        with patch.object(settings, "MAX_TOTAL_ATTACHMENTS_PER_RECORD", 7):
            with self.assertRaises(OSError):
                self.dialog.save_attachments("saved")
        self.assertEqual((destination / "other.txt").read_bytes(), b"other")
        self.assertTrue((self.dialog._staging_dir / "new.txt").exists())

    def test_recognize_existing_attachment_passes_selected_template_without_copying(self):
        existing = self.root / "attachments" / "permit"
        existing.mkdir(parents=True)
        source = existing / "document.png"
        source.write_bytes(b"fixture")
        dialog = EngedelyDialog(
            engedely={"id": "permit"}, attachments_base=str(existing.parent),
            telephelyek=[{"id": "site", "nev": "Site"}],
        )
        self.addCleanup(dialog.deleteLater)
        template = object()
        with patch.object(dialog.ocr_template_selector, "selected_template", return_value=template):
            with patch("gui.engedely_dialog.OcrWorker") as worker:
                dialog._recognize_selected_attachment()
                worker.assert_called_once_with([source], dialog, template=template)
                self.assertFalse(dialog.ocr_template_selector.isEnabled())
                dialog._ocr_finished()
        self.assertTrue(dialog.ocr_template_selector.isEnabled())
        self.assertEqual(dialog.attach_list.count(), 1)
        self.assertFalse(dialog._staging_dir.exists())

    def test_legacy_review_notes_are_preserved(self):
        dialog = EngedelyDialog(
            engedely={"id": "old", "felulvizsgalatok": '[{"date": "2030-01-01", "note": "Original note"}]'},
            attachments_base=str(self.root / "attachments"),
        )
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.get_data()["felulvizsgalatok"], [
            {"type": "date", "date": "2030-01-01", "note": "Original note"},
        ])

    def test_browsing_starts_ocr_and_save_moves_attachment(self):
        source = self.root / "permit.png"
        source.write_bytes(b"fixture")
        result = DocumentFields("Engedély", date(2032, 5, 10), "fixed")
        with patch("gui.engedely_dialog.QFileDialog.getOpenFileNames", return_value=([str(source)], "")):
            with patch("gui.ocr_worker.read_document", return_value=result):
                self.dialog.add_attachment()
                self.assertFalse(self.dialog.save_btn.isEnabled())
                self.wait_for_worker()
        self.assertEqual(self.dialog.targy.text(), "Engedély")
        self.assertEqual(self.dialog.get_data()["lejarat"], "2032-05-10")
        self.assertEqual(self.dialog.attach_list.count(), 1)
        self.dialog.save_attachments("saved")
        self.assertEqual((self.root / "attachments/saved/permit.png").read_bytes(), b"fixture")
        self.assertFalse(self.dialog._staging_dir.exists())

    def test_edits_during_ocr_are_preserved(self):
        self.dialog._ocr_revisions = (self.dialog._subject_revision, self.dialog._validity_revision)
        self.dialog.targy.setText("Kézzel megadott tárgy")
        self.dialog.rb_indef.setChecked(True)
        self.dialog._apply_ocr(DocumentFields("OCR tárgy", date(2032, 1, 1), "fixed"), [])
        self.assertEqual(self.dialog.targy.text(), "Kézzel megadott tárgy")
        self.assertTrue(self.dialog.rb_indef.isChecked())
        self.assertIn("nem írtuk felül", self.dialog.ocr_status.text())

    def test_new_fields_and_full_text_are_available_after_whole_document_read(self):
        result = DocumentFields(
            subject="Engedély", expiry=date(2030, 12, 31), validity="fixed",
            authority="Minta Hatóság", issued_on=date(2026, 11, 1), decision_number="PE/12/2026",
            text="--- 1. oldal ---\nTeljes <eredeti> szöveg\n--- 2. oldal ---\nUtolsó sor.",
        )
        with patch("gui.ocr_worker.read_document", return_value=result):
            self.dialog._start_ocr([self.root / "example.pdf"])
            self.wait_for_worker()
        data = self.dialog.get_data()
        self.assertEqual(data["kiado_hatosag"], "Minta Hatóság")
        self.assertEqual(data["letrejott"], "2026-11-01")
        self.assertEqual(data["hatarozatszam"], "PE/12/2026")
        self.assertEqual(data["engedely_nev"], "Engedély")
        self.assertEqual(data["lejarat"], "2030-12-31")
        self.assertTrue(self.dialog.ocr_text_btn.isEnabled())
        with patch("gui.engedely_dialog.QDialog.exec", return_value=QDialog.Rejected):
            self.dialog._show_recognized_text()
        text = self.dialog.findChild(QTextEdit, "ocrFullText")
        self.assertTrue(text.isReadOnly())
        self.assertIn("Teljes <eredeti> szöveg", text.toPlainText())
        self.assertIn("Utolsó sor.", text.toPlainText())

    def test_manual_authority_date_and_decision_changes_during_ocr_are_preserved(self):
        def recognize(*args, **kwargs):
            time.sleep(.05)
            return DocumentFields(authority="OCR Hatóság", issued_on=date(2026, 11, 1), decision_number="OCR/2026")

        with patch("gui.ocr_worker.read_document", side_effect=recognize):
            self.dialog._start_ocr([self.root / "example.pdf"])
            self.dialog.kiado.setText("Kézi Hatóság")
            self.dialog.hatarozatszam.setText("KEZI/2026")
            self.dialog.keltezes_cb.setChecked(True)
            self.dialog.keltezes_date.setDate(QDate(2025, 2, 3))
            self.wait_for_worker()
        self.assertEqual(self.dialog.get_data()["kiado_hatosag"], "Kézi Hatóság")
        self.assertEqual(self.dialog.get_data()["hatarozatszam"], "KEZI/2026")
        self.assertEqual(self.dialog.get_data()["letrejott"], "2025-02-03")
        self.assertIn("nem írtuk felül", self.dialog._recognized_text)

    def test_ocr_does_not_truncate_long_subject(self):
        self.dialog._ocr_revisions = (self.dialog._subject_revision, self.dialog._validity_revision)
        self.dialog._apply_ocr(DocumentFields(subject="x" * 201), [])
        self.assertEqual(self.dialog.targy.text(), "")
        self.assertIn("túl hosszú", self.dialog.ocr_status.text())

    def test_failed_ocr_keeps_attachment_and_enables_manual_entry(self):
        from core.document_ocr import DocumentReadError
        source = self.root / "bad.pdf"
        source.write_bytes(b"corrupt")
        with patch("gui.engedely_dialog.QFileDialog.getOpenFileNames", return_value=([str(source)], "")):
            with patch("gui.ocr_worker.read_document", side_effect=DocumentReadError("Hibás PDF")):
                with patch.object(QMessageBox, "warning") as warning:
                    self.dialog.add_attachment()
                    self.wait_for_worker()
                    warning.assert_called_once()
        self.assertTrue(self.dialog.save_btn.isEnabled())
        self.assertEqual(self.dialog.attach_list.count(), 1)
        self.assertIn("sikertelen", self.dialog.ocr_status.text())

    def test_reject_during_ocr_waits_without_destroying_thread(self):
        source = self.root / "permit.png"
        source.write_bytes(b"fixture")

        def delayed_result(*args, **kwargs):
            time.sleep(0.05)
            return DocumentFields(subject="Must not apply")

        with patch("gui.engedely_dialog.QFileDialog.getOpenFileNames", return_value=([str(source)], "")):
            with patch("gui.ocr_worker.read_document", side_effect=delayed_result):
                self.dialog.add_attachment()
                self.dialog.reject()
                self.wait_for_worker()
        self.assertEqual(self.dialog.result(), QDialog.Rejected)
        self.assertEqual(self.dialog.targy.text(), "")
        self.assertFalse(self.dialog._staging_dir.exists())

    def test_cancel_edit_preserves_existing_files(self):
        existing = self.root / "attachments/existing"
        existing.mkdir(parents=True)
        path = existing / "old.txt"
        path.write_text("original")
        dialog = EngedelyDialog(engedely={"id": "existing"}, attachments_base=str(existing.parent))
        self.addCleanup(dialog.deleteLater)
        dialog.attach_list.setCurrentRow(0)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            dialog.remove_attachment()
        self.assertTrue(path.exists())
        source = self.root / "new.txt"
        source.write_text("new")
        with patch("gui.engedely_dialog.QFileDialog.getOpenFileNames", return_value=([str(source)], "")):
            dialog.add_attachment()
        dialog.reject()
        self.assertEqual(list(existing.iterdir()), [path])
        self.assertFalse(dialog._staging_dir.exists())

    def test_edit_save_applies_staged_removal_and_duplicate_names(self):
        existing = self.root / "attachments/existing"
        existing.mkdir(parents=True)
        (existing / "same.txt").write_text("original")
        removed = existing / "remove.txt"
        removed.write_text("remove")
        dialog = EngedelyDialog(engedely={"id": "existing"}, attachments_base=str(existing.parent))
        self.addCleanup(dialog.deleteLater)
        for i in range(dialog.attach_list.count()):
            if dialog.attach_list.item(i).data(Qt.UserRole) == removed:
                dialog.attach_list.setCurrentRow(i)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            dialog.remove_attachment()
        source = self.root / "same.txt"
        source.write_text("new")
        with patch("gui.engedely_dialog.QFileDialog.getOpenFileNames", return_value=([str(source)], "")):
            dialog.add_attachment()
        dialog.save_attachments("existing")
        self.assertFalse(removed.exists())
        self.assertEqual((existing / "same.txt").read_text(), "original")
        self.assertEqual((existing / "same_1.txt").read_text(), "new")

    def test_retry_after_staging_unlink_failure_does_not_duplicate_copy(self):
        self.dialog._staging_dir.mkdir(parents=True)
        staged = self.dialog._staging_dir / "permit.txt"
        staged.write_text("original")
        unlink = Path.unlink

        def fail_staged_unlink(path, *args, **kwargs):
            if path == staged:
                raise OSError("Staging temporarily unavailable")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", fail_staged_unlink):
            with self.assertRaises(OSError):
                self.dialog.save_attachments("saved")
        self.dialog.save_attachments("saved")
        saved = self.root / "attachments/saved"
        self.assertEqual([p.name for p in saved.iterdir()], ["permit.txt"])

    def test_size_and_extension_limits_apply_at_browse(self):
        source = self.root / "script.exe"
        source.write_bytes(b"not executable")
        with patch("gui.engedely_dialog.QFileDialog.getOpenFileNames", return_value=([str(source)], "")):
            with patch.object(QMessageBox, "warning") as warning:
                self.dialog.add_attachment()
                warning.assert_called_once()
        self.assertEqual(self.dialog.attach_list.count(), 0)
        source = self.root / "large.txt"
        source.write_bytes(b"1234")
        for setting in ("MAX_ATTACHMENT_SIZE", "MAX_TOTAL_ATTACHMENTS_PER_RECORD"):
            with patch("gui.engedely_dialog.QFileDialog.getOpenFileNames", return_value=([str(source)], "")):
                with patch(f"utils.fs_utils.settings.{setting}", 3):
                    with patch.object(QMessageBox, "warning") as warning:
                        self.dialog.add_attachment()
                        warning.assert_called_once()
            self.assertEqual(self.dialog.attach_list.count(), 0)

    def test_expiry_change_refreshes_review_and_enables_date_correctly(self):
        self.dialog.felulvizsgalatok = [{"type": "before_expiry", "days": 30, "note": ""}]
        self.dialog.hatary_date.setDate(QDate(2090, 1, 31))
        self.assertIn("2090-01-01", self.dialog.next_review_lbl.text())
        self.dialog.rb_indef.setChecked(True)
        self.assertFalse(self.dialog.hatary_date.isEnabled())
        self.dialog.rb_fixed.setChecked(True)
        self.assertTrue(self.dialog.hatary_date.isEnabled())

    def test_removing_all_reviews_clears_saved_list(self):
        self.dialog.felulvizsgalatok = []
        self.assertEqual(self.dialog.get_data()["felulvizsgalatok"], [])
