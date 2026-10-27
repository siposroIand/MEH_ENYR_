import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication, QDialog
from gui.auth_dialogs import LoginDialog, ProfileDialog, UsersDialog


class FakeAuth:
    def __init__(self, bootstrap=False):
        self.bootstrap = bootstrap
        self.calls = []
        self.session = None

    def needs_bootstrap(self):
        return self.bootstrap

    @property
    def current_user(self):
        return {
            "id": "test", "username": "teszt", "display_name": "Teszt",
            "role": "admin", "active": 1,
        }

    def login(self, username, password):
        self.calls.append(("login", username, password))
        self.session = object()

    def bootstrap_admin(self, username, password, display_name):
        self.calls.append(("bootstrap", username, password, display_name))
        self.session = object()

    def update_profile(self, name):
        self.calls.append(("profile", name))

    def change_password(self, old, new, **kwargs):
        self.calls.append(("password", old, new))
        self.password_profile_name = kwargs.get("display_name")
        self.session = None

    def list_users(self):
        return [self.current_user]


class AuthDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def wait_finished(self, dialog, operation):
        loop = QEventLoop()
        dialog.finished.connect(loop.quit)
        operation()
        QTimer.singleShot(5000, loop.quit)
        loop.exec()
        self.assertEqual(dialog.result(), QDialog.Accepted)

    def test_login_runs_worker_and_clears_password(self):
        auth = FakeAuth()
        dialog = LoginDialog(auth)
        dialog.username.setText("teszt")
        dialog.password.setText("not-a-real-credential")
        self.wait_finished(dialog, dialog._submit)
        self.assertEqual(auth.calls[0][0], "login")
        self.assertEqual(dialog.password.text(), "")
        self.assertIsNotNone(auth.session)

    def test_bootstrap_mismatch_does_not_call_service(self):
        auth = FakeAuth(True)
        dialog = LoginDialog(auth)
        dialog.password.setText("first-value")
        dialog.confirm.setText("second-value")
        with patch("gui.auth_dialogs.QMessageBox.warning") as warning:
            dialog._submit()
        warning.assert_called_once()
        self.assertFalse(auth.calls)

    def test_bootstrap_collects_first_admin_fields(self):
        auth = FakeAuth(True)
        dialog = LoginDialog(auth)
        dialog.username.setText("teszt")
        dialog.display_name.setText("Teszt")
        dialog.password.setText("not-a-real-credential")
        dialog.confirm.setText(dialog.password.text())
        self.wait_finished(dialog, dialog._submit)
        self.assertEqual(auth.calls[0][0], "bootstrap")
        self.assertEqual(auth.calls[0][-1], "Teszt")

    def test_profile_name_change_uses_profile_service(self):
        auth = FakeAuth()
        dialog = ProfileDialog(auth)
        dialog.name.setText("Másik név")
        self.wait_finished(dialog, dialog._save)
        self.assertEqual(auth.calls, [("profile", "Másik név")])

    def test_password_change_requires_new_login(self):
        auth = FakeAuth()
        auth.session = object()
        dialog = ProfileDialog(auth)
        dialog.old.setText("old-test-value")
        dialog.new.setText("new-test-value")
        dialog.confirm.setText("new-test-value")
        dialog.name.setText("Új profilnév")
        self.wait_finished(dialog, dialog._save)
        self.assertEqual(auth.calls[0][0], "password")
        self.assertIsNone(auth.session)
        self.assertEqual(auth.password_profile_name, "Új profilnév")

    def test_user_table_never_displays_hash(self):
        dialog = UsersDialog(FakeAuth())
        self.assertEqual(dialog.table.columnCount(), 4)
        self.assertEqual(dialog.table.item(0, 0).text(), "teszt")
        self.assertEqual(dialog.table.item(0, 3).text(), "Aktív")


if __name__ == "__main__":
    unittest.main()
