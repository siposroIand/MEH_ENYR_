import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QThread, QTimer, Qt
from PySide6.QtWidgets import QApplication, QWidget
from gui.report_builder import ReportBuilderDialog, run_report
from gui.workers import wait_for_tasks


class ReportFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.parent = QWidget()
        self.data = {"engedelyek": [
            {"id": "active", "engedely_nev": "Aktív", "archivalt": 0},
            {"id": "archive", "engedely_nev": "Archív", "archivalt": 1},
        ]}

    def tearDown(self):
        wait_for_tasks()
        self.app.processEvents()
        self.parent.deleteLater()

    def _export(self, options=None, fail=False):
        loop = QEventLoop()
        calls, messages, ticks = [], [], []

        def exporter(*args, **kwargs):
            calls.append((args, kwargs, QThread.currentThread() == self.app.thread()))
            time.sleep(0.04)
            if fail:
                raise OSError("Nincs írási jog")

        def message(*args):
            messages.append((args, QThread.currentThread() == self.app.thread()))
            loop.quit()

        timer = QTimer()
        timer.setInterval(2)
        timer.timeout.connect(lambda: ticks.append(1))
        with patch("gui.report_builder._import_exporters", return_value=(exporter, [])), \
                patch("gui.report_builder.QFileDialog.getSaveFileName", return_value=("/tmp/report.pdf", "")), \
                patch("gui.report_builder.QFileDialog.getExistingDirectory", return_value="/tmp"), \
                patch("PySide6.QtWidgets.QMessageBox.information", side_effect=message), \
                patch("PySide6.QtWidgets.QMessageBox.critical", side_effect=message):
            delivery = run_report(self.parent, options or {"format": "PDF"}, self.data, [])
            timer.start()
            QTimer.singleShot(5000, loop.quit)
            loop.exec()
            timer.stop()
        self.assertIsNotNone(delivery)
        self.assertTrue(ticks, "Az export alatt is futnia kell a GUI eseményciklusának.")
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0][1])
        self.assertFalse(calls[0][2], "Az exportáló a munkaszálban fusson.")
        return calls[0], messages[0][0]

    def test_pdf_runs_in_worker_and_honors_hungarian_archive_field(self):
        (args, kwargs, _), _ = self._export()
        self.assertEqual([row["id"] for row in args[1]["Összes"]], ["active"])
        self.assertEqual(kwargs["fmt"], "PDF")

    def test_per_group_excel_preserves_include_archived(self):
        (args, kwargs, _), _ = self._export({
            "format": "Excel", "per_group": True, "include_archived": True,
            "columns": ["engedely_nev"],
        })
        self.assertEqual(args[0], "/tmp")
        self.assertEqual(len(args[1]["Összes"]), 2)
        self.assertEqual(args[3], ["engedely_nev"])
        self.assertEqual(kwargs["fmt"], "XLSX")
        self.assertTrue(kwargs["per_group"])

    def test_export_failure_returns_gui_message_with_partial_output_warning(self):
        _, message = self._export(fail=True)
        self.assertIn("Nincs írási jog", message[2])
        self.assertIn("megmaradhattak", message[2])

    def test_cancel_file_selection_does_not_dispatch_worker(self):
        with patch("gui.report_builder.QFileDialog.getSaveFileName", return_value=("", "")), \
                patch("gui.workers.run_task") as task:
            self.assertIsNone(run_report(self.parent, {}, self.data, []))
        task.assert_not_called()

    def test_preview_offers_responsible_and_resolves_typed_reviews(self):
        data = {"engedelyek": [{
            "id": "review", "hataly_tipus": "fixed", "lejarat": "2030-06-30",
            "kategoria": "Egyéb engedély",
            "responsible_name": "Ügyintéző", "archivalt": 0,
            "felulvizsgalatok": [{"type": "before_expiry", "days": 0}],
        }]}
        dialog = ReportBuilderDialog(self.parent, data=data)
        self.assertIn("responsible_name", dialog._col_items)
        self.assertNotIn("password_hash", dialog._col_items)
        for key, item in dialog._col_items.items():
            item.setCheckState(Qt.Checked if key in {"responsible_name", "felulvizsgalat"} else Qt.Unchecked)
        dialog._update_preview_and_summary()
        values = [dialog.preview_table.item(0, column).text()
                  for column in range(dialog.preview_table.columnCount())]
        self.assertIn("Ügyintéző", values)
        self.assertIn("2030.06.30", values)
        dialog.reject()


if __name__ == "__main__":
    unittest.main()
