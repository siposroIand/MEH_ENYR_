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
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from core.db import Database
from core.document_ocr import DocumentFields
from gui.engedely_dialog import EngedelyDialog
from gui.main_window import MainWindow
from gui.telephely_list_dialog import TelephelyListDialog


class PermitPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "records.db")
        with self.db.get_connection() as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('site', 'Telephely')")
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
        self.assertEqual(self.db.get_catalogs()["engedelytipusok"], [])

    def test_normalized_catalogs_are_reused_and_joined_on_read(self):
        self.data.update(kategoria=" Egyedi típus ", kiado_hatosag=" Egyedi hatóság ")
        for identifier in ("first", "second"):
            MainWindow._persist_permit(self.owner, self.data, identifier, "2026-01-01T12:00:00")
        catalogs = self.db.get_catalogs()
        self.assertEqual(len(catalogs["hatosagok"]), 1)
        self.assertEqual(len(catalogs["engedelytipusok"]), 1)
        for row in self.db.get_changes("2026-01-01T00:00:00"):
            self.assertEqual(row["kategoria"], "Egyedi típus")
            self.assertEqual(row["kiado_hatosag"], "Egyedi hatóság")
            self.assertEqual(row["engedelytipus_id"], catalogs["engedelytipusok"][0]["id"])
        self.data.update(kategoria="Másik típus", kiado_hatosag=None)
        MainWindow._persist_permit(self.owner, self.data, "first", "2026-01-02T00:00:00", updating=True)
        record = self.db.get_changes("2026-01-02T00:00:00")[0]
        self.assertEqual(record["kategoria"], "Másik típus")
        self.assertIsNone(record["kiado_hatosag"])

    def test_invalid_site_rejected_and_catalog_insert_rolled_back(self):
        self.data["telephely_id"] = "missing"
        self.data["kiado_hatosag"] = "New authority"
        with self.assertRaises(sqlite3.IntegrityError):
            MainWindow._persist_permit(self.owner, self.data, "permit", "2026-01-01T12:00:00")
        self.assertEqual(self.db.get_all_data()["engedelyek"], [])
        self.assertEqual(self.db.get_catalogs()["hatosagok"], [])


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

    def test_custom_category_is_saved_filterable_and_available_after_restart(self):
        self.dialog.kategoria.setCurrentText("Levegőtisztaság-védelmi engedély")
        self.dialog.kiado.setText("Új hatóság")
        with patch("gui.main_window.EngedelyDialog", return_value=self.dialog):
            with patch.object(self.dialog, "exec", return_value=QDialog.Accepted):
                self.window.add_engedely()
        category = self.dialog.kategoria.currentText()
        self.assertIn(category, self.window.get_category_suggestions())
        self.assertIn("Új hatóság", self.window.get_hatosag_suggestions())
        self.window.kategoria_filter.set_checked_by_data([category])
        self.window.refresh_data(incremental=False)
        self.assertEqual(self.window.kategoria_filter.checked_texts(), [category])
        self.assertEqual(len(self.window._get_filtered_items()), 1)
        reopened = Database(self.window.db.db_path).get_all_data()
        self.assertEqual(reopened["engedelytipusok"][0]["nev"], category)

    def test_site_delete_detaches_only_deleted_permits(self):
        with self.window.db.get_connection() as conn:
            conn.execute("INSERT INTO engedelyek (id, telephely_id, deleted) VALUES ('deleted', 'site', 1)")
        manager = TelephelyListDialog(self.window.db, self.window)
        self.addCleanup(manager.deleteLater)
        manager.listw.setCurrentRow(0)
        with patch("gui.telephely_list_dialog.QMessageBox.question", return_value=QMessageBox.Yes):
            with patch("gui.telephely_list_dialog.QMessageBox.critical") as error:
                manager.delete_selected()
        error.assert_not_called()
        self.assertEqual(self.window.db.get_telephelyek(), [])
        with self.window.db.get_connection() as conn:
            row = conn.execute("SELECT id, telephely_id, deleted FROM engedelyek").fetchone()
            self.assertEqual(tuple(row), ("deleted", None, 1))
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
