from copy import deepcopy
from datetime import date, timedelta
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QTabWidget

from gui.main_window import MainWindow


class MainWindowRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Mock()
        self.db.get_all_data.return_value = {"telephelyek": [], "engedelyek": []}
        self.window = MainWindow(self.db, {"attachments": self.root})
        for timer in self.window.findChildren(QTimer):
            timer.stop()
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self.close_popup)

    def close_popup(self):
        if self.window._expiry_dialog is not None:
            self.window._expiry_dialog.close()

    def permit(self, identifier, days, **extra):
        return {
            "id": identifier, "lejarat": (date.today() + timedelta(days=days)).isoformat(),
            "telephely_id": "site", "hataly_tipus": "fixed", **extra,
        }

    def test_expiry_boundaries_and_exclusions(self):
        self.window.data = {
            "telephelyek": [{"id": "site", "nev": "Telephely"}],
            "engedelyek": [
                self.permit(str(days), days) for days in (-1, 0, 90, 91, 180, 181)
            ] + [
                self.permit("archived", 1, archivalt=1),
                self.permit("deleted", 1, deleted=1),
                self.permit("indef", 1, hataly_tipus="indefinite"),
                self.permit("until", 1, hataly_tipus="until_modified"),
            ],
        }
        groups = self.window._expiring_groups()
        self.assertEqual([[e["id"] for e in group] for group in groups], [["91", "180"], ["0", "90"], ["-1"]])
        self.assertTrue(all(e["telephely_nev"] == "Telephely" for group in groups for e in group))

    def test_popup_shown_once_per_day_and_for_new_permits(self):
        self.window.data["engedelyek"] = [self.permit("first", -1)]
        self.window.check_expiring_permits()
        popup = self.window._expiry_dialog
        self.assertTrue(popup.isVisible())
        self.assertEqual(popup.findChild(QTabWidget).currentIndex(), 2)
        self.window.check_expiring_permits()
        self.assertIs(self.window._expiry_dialog, popup)
        popup.close()
        self.assertIsNone(self.window._expiry_dialog)
        self.window.check_expiring_permits()
        self.assertIsNone(self.window._expiry_dialog)
        self.window.data["engedelyek"].append(self.permit("new", 45))
        self.window.check_expiring_permits()
        self.assertIsNotNone(self.window._expiry_dialog)
        self.close_popup()
        self.window._expiry_notification_day = date.today() - timedelta(days=1)
        self.window.check_expiring_permits()
        self.assertIsNotNone(self.window._expiry_dialog)

    def test_modal_editing_defers_notifications(self):
        self.window.data["engedelyek"] = [self.permit("first", 1)]
        modal = QDialog(self.window)
        with patch("gui.main_window.QApplication.activeModalWidget", return_value=modal):
            self.window.check_expiring_permits()
        self.assertIsNone(self.window._expiry_dialog)
        self.assertEqual(self.window._expiry_notified, set())
        self.window.check_expiring_permits()
        self.assertIsNotNone(self.window._expiry_dialog)

    def test_startup_timer_and_refresh_are_wired(self):
        self.assertTrue(self.window._expiry_startup_timer.isSingleShot())
        self.window.data["engedelyek"] = [self.permit("first", 2)]
        self.window._expiry_startup_timer.timeout.emit()
        self.assertIsNotNone(self.window._expiry_dialog)
        with patch.object(self.window, "check_expiring_permits") as check:
            with patch.object(self.window, "refresh_data"):
                with patch.object(self.window.global_search, "hasFocus", return_value=True):
                    self.window.silent_refresh()
                self.window.manual_refresh()
        self.assertEqual(check.call_count, 2)

    def test_failed_sync_keeps_data_and_cursor_then_recovers(self):
        self.window.data["engedelyek"] = [self.permit("first", 1)]
        original = deepcopy(self.window.data)
        cursor = self.window.last_sync
        self.db.get_telephelyek.return_value = []
        self.db.get_changes.side_effect = sqlite3.OperationalError("unavailable")
        self.window.silent_refresh()
        self.assertEqual(self.window.data, original)
        self.assertEqual(self.window.last_sync, cursor)
        self.assertTrue(self.window.statusBar().currentMessage())
        self.db.get_changes.side_effect = None
        self.db.get_changes.return_value = []
        self.window.silent_refresh()
        self.assertEqual(self.window.statusBar().currentMessage(), "")
        self.assertIsNotNone(self.window._expiry_dialog)

    def test_incremental_deletion_removes_cached_permit(self):
        self.window.data["engedelyek"] = [self.permit("deleted", 1), self.permit("keep", 180)]
        self.db.get_telephelyek.return_value = []
        self.db.get_changes.return_value = [{"id": "deleted", "deleted": 1}]
        self.window.refresh_data()
        self.assertEqual([e["id"] for e in self.window.data["engedelyek"]], ["keep"])

    def test_refresh_does_not_scan_attachment_directories(self):
        self.window.data["engedelyek"] = [
            self.permit(str(index), 200, kategoria="Egyéb engedély") for index in range(1000)
        ]
        with patch.object(Path, "iterdir", side_effect=AssertionError("No filesystem scans during filtering")):
            self.window.refresh_table()
        self.assertEqual(self.window.table.rowCount(), 1000)
        self.assertEqual(self.window.table.item(999, 8).text(), "Megnyitás")
        self.assertTrue(self.window.table.updatesEnabled())

    def test_single_file_or_multiple_file_folder_opened(self):
        folder = self.root / "permit"
        folder.mkdir()
        file = folder / "permit.pdf"
        file.write_bytes(b"fixture")
        with patch("gui.main_window.open_local_path") as opener:
            self.window._open_with_preference({"id": "permit"})
            opener.assert_called_with(file)
            (folder / "second.pdf").write_bytes(b"fixture")
            self.window._open_with_preference({"id": "permit"})
            opener.assert_called_with(folder)

    def test_mixed_timestamp_sorting_is_stable(self):
        self.db.get_all_data.return_value = {
            "telephelyek": [], "engedelyek": [
                self.permit("empty", 10),
                self.permit("naive", 10, last_modified="2026-01-01T12:00:00"),
                self.permit("aware", 10, last_modified="2026-01-01T13:00:01+01:00"),
            ],
        }
        self.window.refresh_data(incremental=False)
        self.assertEqual([e["id"] for e in self.window.data["engedelyek"]], ["aware", "naive", "empty"])
