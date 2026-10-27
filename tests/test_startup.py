import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, QThread
from PySide6.QtWidgets import QApplication, QWidget
from config.paths import build_app_dirs
from gui.workers import run_task, wait_for_tasks


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_existing_absolute_root_preserves_directory_layout(self):
        with tempfile.TemporaryDirectory() as root:
            dirs = build_app_dirs(Path(root))
            self.assertEqual(dirs["db"], Path(root) / "db")
            self.assertEqual(dirs["attachments"], Path(root) / "attachment")
            self.assertTrue(all(path.is_dir() for path in dirs.values()))

    def test_missing_share_does_not_create_local_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            missing = Path(root) / "unavailable"
            with self.assertRaises(RuntimeError):
                build_app_dirs(missing)
            self.assertFalse(missing.exists())
        with self.assertRaises(RuntimeError):
            build_app_dirs(Path("relative-share"))

    def test_directory_creation_errors_propagate(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "db").write_text("not a directory")
            with self.assertRaises(FileExistsError):
                build_app_dirs(Path(root))

    def test_worker_callbacks_are_delivered_on_gui_thread(self):
        parent = QWidget()
        loop = QEventLoop()
        results = []

        def finish(value):
            results.append((value, QThread.currentThread() == self.app.thread()))
            loop.quit()

        delivery = run_task(parent, lambda: 42, finish, finish)
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        self.assertEqual(results, [(42, True)])
        self.assertIsNotNone(delivery)
        parent.deleteLater()

    def test_worker_error_is_delivered_without_raising_on_gui_thread(self):
        parent = QWidget()
        loop = QEventLoop()
        errors = []

        def failure(error):
            errors.append(error)
            loop.quit()

        def work():
            raise ValueError("teszt")

        run_task(parent, work, lambda result: loop.quit(), failure)
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError)
        parent.deleteLater()

    def test_destroyed_owner_never_receives_callback(self):
        parent = QWidget()
        results = []
        run_task(parent, lambda: 7, results.append, results.append)
        parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        wait_for_tasks()
        self.app.processEvents()
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
