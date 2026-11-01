import json
import os
from datetime import date
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog

from core.db import Database
from core.document_ocr import DocumentFields
from gui.engedely_dialog import EngedelyDialog
from gui.main_window import MainWindow


class PermitPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "records.db")
        self.db.save_telephely({"nev": "Telephely"}, "site")
        self.owner = SimpleNamespace(db=self.db)
        self.data = {
            "telephely_id": "site", "engedely_nev": "OCR tárgy",
            "hatarozatszam": "123", "kategoria": "Egyéb engedély",
            "hataly_tipus": "fixed", "lejarat": "2030-12-31",
            "hatarozatlan": 0,
            "felulvizsgalatok": [{"type": "before_expiry", "days": 30, "note": "Review"}],
        }

    def test_create_update_and_clear_reviews(self):
        MainWindow._persist_permit(self.owner, self.data, "permit", "2026-01-01T12:00:00")
        record = self.db.get_all_data()["engedelyek"][0]
        self.assertEqual(record["engedely_nev"], "OCR tárgy")
        self.assertEqual(record["lejarat"], "2030-12-31")
        self.assertEqual(json.loads(record["felulvizsgalatok"]), self.data["felulvizsgalatok"])
        self.data["felulvizsgalatok"] = []
        self.data["engedely_nev"] = "Updated"
        self.data["revision"] = record["revision"]
        MainWindow._persist_permit(self.owner, self.data, "permit", "2026-01-02T12:00:00", updating=True)
        record = self.db.get_all_data()["engedelyek"][0]
        self.assertEqual(record["engedely_nev"], "Updated")
        self.assertEqual(record["felulvizsgalatok"], "[]")

    def test_deleted_record_update_is_not_reported_as_saved(self):
        MainWindow._persist_permit(self.owner, self.data, "permit", "2026-01-01T12:00:00")
        with self.db.get_connection() as conn:
            conn.execute("UPDATE engedelyek SET deleted=1 WHERE id='permit'")
        with self.assertRaises(sqlite3.IntegrityError):
            MainWindow._persist_permit(self.owner, self.data, "permit", "2026-01-02T12:00:00", updating=True)

    def test_missing_record_update_is_not_reported_as_saved(self):
        with self.assertRaises(sqlite3.IntegrityError):
            MainWindow._persist_permit(self.owner, self.data, "missing", "2026-01-02T12:00:00", updating=True)


class MainWindowSaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        db = Database(self.root / "records.db")
        with db.get_connection() as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('site', 'Telephely')")
        self.window = MainWindow(db, {"attachments": self.root / "attachments"})
        for timer in self.window.findChildren(QTimer):
            timer.stop()
        self.addCleanup(self.window.deleteLater)
        self.dialog = EngedelyDialog(
            self.window, telephelyek=db.get_telephelyek(),
            attachments_base=str(self.root / "attachments"),
        )
        self.dialog.hatarozatszam.setText("123/2026")
        self.dialog._ocr_revisions = (self.dialog._subject_revision, self.dialog._validity_revision)
        self.dialog._apply_ocr(DocumentFields("OCR tárgy", date(2030, 12, 31), "fixed"), [])
        self.dialog._staging_dir.mkdir()
        (self.dialog._staging_dir / "permit.txt").write_text("attachment")

    def test_new_ocr_values_are_immediately_displayed_and_searchable(self):
        with patch("gui.main_window.EngedelyDialog", return_value=self.dialog):
            with patch.object(self.dialog, "exec", return_value=QDialog.Accepted):
                self.window.add_engedely()
        record = self.window.data["engedelyek"][0]
        self.assertEqual(record["_lej_dt"], date(2030, 12, 31))
        self.assertIn("ocr tárgy", record["_search_index"])
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertEqual(
            (self.root / "attachments" / record["id"] / "permit.txt").read_text(), "attachment",
        )

    def test_database_failure_reuses_form_and_staged_files(self):
        persist = self.window._persist_permit
        calls = []

        def fail_once(data, entity_id, now, updating=False):
            calls.append(entity_id)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return persist(data, entity_id, now, updating)

        with patch("gui.main_window.EngedelyDialog", return_value=self.dialog):
            with patch.object(self.dialog, "exec", return_value=QDialog.Accepted) as execute:
                with patch.object(self.window, "_persist_permit", side_effect=fail_once):
                    with patch("gui.main_window.error") as error:
                        self.window.add_engedely()
                        error.assert_called_once()
                self.assertEqual(execute.call_count, 2)
        self.assertEqual(calls[0], calls[1])
        record = self.window.db.get_all_data()["engedelyek"][0]
        self.assertEqual(record["engedely_nev"], "OCR tárgy")
        self.assertTrue((self.root / "attachments" / record["id"] / "permit.txt").exists())

    def test_report_export_uses_same_snapshot_as_preview_during_remote_refresh(self):
        snapshot = self.window.data
        with patch("gui.main_window.ReportBuilderDialog") as builder:
            dialog = builder.return_value
            dialog.get_options.return_value = {"title": "Snapshot report"}

            def refresh_while_open():
                self.window.data = {"telephelyek": [], "engedelyek": []}
                return QDialog.Accepted

            dialog.exec.side_effect = refresh_while_open
            with patch("gui.main_window.run_report") as export:
                self.window.open_report_builder()
            self.assertIs(builder.call_args.kwargs["data"], snapshot)
            self.assertIs(export.call_args.args[2], snapshot)

    def test_whole_document_ocr_fields_are_saved_in_existing_columns(self):
        self.dialog._ocr_revisions = (self.dialog._subject_revision, self.dialog._validity_revision)
        self.dialog._ocr_extra_revisions = self.dialog._extra_revisions.copy()
        self.dialog._apply_ocr(DocumentFields(
            subject="Teljes dokumentumból", expiry=date(2031, 6, 30), validity="fixed",
            authority="Minta Hatóság", issued_on=date(2026, 11, 1), decision_number="PE/12/2026",
        ), [])
        with patch("gui.main_window.EngedelyDialog", return_value=self.dialog):
            with patch.object(self.dialog, "exec", return_value=QDialog.Accepted):
                self.window.add_engedely()
        stored = self.window.db.get_all_data()["engedelyek"][0]
        self.assertEqual(stored["kiado_hatosag"], "Minta Hatóság")
        self.assertEqual(stored["letrejott"], "2026-11-01")
        self.assertEqual(stored["hatarozatszam"], "PE/12/2026")
        self.assertEqual(stored["engedely_nev"], "Teljes dokumentumból")
        self.assertEqual(stored["lejarat"], "2031-06-30")
