import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog
from main import ApplicationController


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_cancel_login_never_constructs_main_window(self):
        controller = ApplicationController(self.app)
        controller.service = object()
        with patch("main.LoginDialog") as login, patch.object(self.app, "quit") as quit_app:
            login.return_value.exec.return_value = QDialog.Rejected
            controller.login()
        self.assertIsNone(controller.window)
        quit_app.assert_called_once()

    def test_unavailable_login_db_is_a_controlled_startup_error(self):
        controller = ApplicationController(self.app)
        controller.service = object()
        with patch("main.LoginDialog", side_effect=RuntimeError("Nem elérhető")), \
                patch("main.QMessageBox.critical") as message, \
                patch.object(self.app, "quit") as quit_app:
            controller.login()
        self.assertEqual(controller.exit_code, 1)
        message.assert_called_once()
        quit_app.assert_called_once()

    def test_shutdown_clears_session_even_when_logout_fails(self):
        controller = ApplicationController(self.app)

        class OfflineService:
            session = object()

            def logout(self):
                raise OSError("Megosztás nem érhető el")

        controller.service = OfflineService()
        controller.shutdown()
        self.assertIsNone(controller.service.session)


if __name__ == "__main__":
    unittest.main()
