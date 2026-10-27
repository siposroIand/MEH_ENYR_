from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from core.auth import ROLE_LABELS
from gui.workers import run_task


def _password_edit():
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.Password)
    edit.setMaxLength(1024)
    return edit


class _AsyncDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False

    def _run(self, function, success):
        self._busy = True
        self.setEnabled(False)

        def finish(result):
            self._busy = False
            self.setEnabled(True)
            success(result)

        def failed(error):
            self._busy = False
            self.setEnabled(True)
            QMessageBox.warning(self, "A művelet nem sikerült", str(error))

        run_task(self, function, finish, failed)

    def reject(self):
        if not self._busy:
            super().reject()

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
        else:
            super().closeEvent(event)


class LoginDialog(_AsyncDialog):
    def __init__(self, auth, parent=None):
        super().__init__(parent)
        self.auth = auth
        self.bootstrap = auth.needs_bootstrap()
        self.setWindowTitle("Első rendszergazda" if self.bootstrap else "Bejelentkezés")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Hozza létre az első rendszergazdát. Nincs alapértelmezett jelszó."
            if self.bootstrap else "MEH – Engedélynyilvántartó"
        ))
        form = QFormLayout()
        self.username = QLineEdit()
        self.username.setMaxLength(128)
        self.display_name = QLineEdit()
        self.display_name.setMaxLength(256)
        self.password = _password_edit()
        self.confirm = _password_edit()
        form.addRow("Felhasználónév:", self.username)
        if self.bootstrap:
            form.addRow("Megjelenítési név:", self.display_name)
        form.addRow("Jelszó:", self.password)
        if self.bootstrap:
            form.addRow("Jelszó ismét:", self.confirm)
            form.addRow(QLabel("Jelszó: legalább 10 karakter."))
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Létrehozás" if self.bootstrap else "Belépés")
        buttons.button(QDialogButtonBox.Cancel).setText("Kilépés")
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.password.returnPressed.connect(self._submit)

    def _submit(self):
        if self._busy:
            return
        username, password = self.username.text(), self.password.text()
        name = self.display_name.text().strip()
        if self.bootstrap and password != self.confirm.text():
            QMessageBox.warning(self, "Ellenőrzés", "A két jelszó nem egyezik.")
            return
        function = (
            lambda: self.auth.bootstrap_admin(username, password, name)
        ) if self.bootstrap else lambda: self.auth.login(username, password)

        def done(_):
            self.password.clear()
            self.confirm.clear()
            self.accept()

        self._run(function, done)


class ProfileDialog(_AsyncDialog):
    def __init__(self, auth, parent=None):
        super().__init__(parent)
        self.auth = auth
        user = auth.current_user
        self.setWindowTitle("Saját profil és jelszó")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("Felhasználónév:", QLabel(user["username"]))
        form.addRow("Szerepkör:", QLabel(ROLE_LABELS[user["role"]]))
        self.name = QLineEdit(user["display_name"])
        self.name.setMaxLength(256)
        form.addRow("Megjelenítési név:", self.name)
        self.old = _password_edit()
        self.new = _password_edit()
        self.confirm = _password_edit()
        form.addRow("Jelenlegi jelszó:", self.old)
        form.addRow("Új jelszó:", self.new)
        form.addRow("Új jelszó ismét:", self.confirm)
        layout.addLayout(form)
        layout.addWidget(QLabel("Jelszócsere után új bejelentkezés szükséges."))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        name = self.name.text().strip()
        old, new = self.old.text(), self.new.text()
        if new and new != self.confirm.text():
            QMessageBox.warning(self, "Ellenőrzés", "A két új jelszó nem egyezik.")
            return

        def work():
            if new:
                # Verify the password before changing any profile data.
                self.auth.change_password(old, new, display_name=name)
            else:
                self.auth.update_profile(name)

        self._run(work, lambda _: self.accept())


class _UserDialog(_AsyncDialog):
    def __init__(self, auth, user=None, parent=None):
        super().__init__(parent)
        self.auth, self.user = auth, user
        self.setWindowTitle("Felhasználó módosítása" if user else "Új felhasználó")
        form = QFormLayout(self)
        self.username = QLineEdit((user or {}).get("username", ""))
        self.username.setMaxLength(128)
        self.name = QLineEdit((user or {}).get("display_name", ""))
        self.name.setMaxLength(256)
        self.role = QComboBox()
        for key, label in ROLE_LABELS.items():
            self.role.addItem(label, key)
        self.role.setCurrentIndex(self.role.findData((user or {}).get("role", "viewer")))
        self.active = QCheckBox("Aktív")
        self.active.setChecked(bool((user or {}).get("active", True)))
        self.password = _password_edit()
        self.confirm = _password_edit()
        form.addRow("Felhasználónév:", self.username)
        form.addRow("Név:", self.name)
        form.addRow("Szerepkör:", self.role)
        form.addRow(self.active)
        form.addRow("Új jelszó (10+ karakter):", self.password)
        form.addRow("Jelszó ismét:", self.confirm)
        if user:
            form.addRow(QLabel("Üres jelszómező: a jelenlegi jelszó megmarad."))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _save(self):
        values = {
            "username": self.username.text().strip(),
            "display_name": self.name.text().strip(),
            "role": self.role.currentData(),
            "active": int(self.active.isChecked()),
        }
        password = self.password.text()
        if password != self.confirm.text():
            QMessageBox.warning(self, "Ellenőrzés", "A két jelszó nem egyezik.")
            return
        if self.user:
            values["id"] = self.user["id"]
        if password or not self.user:
            values["password"] = password
        self._run(lambda: self.auth.save_user(values), lambda _: self.accept())


class UsersDialog(QDialog):
    def __init__(self, auth, parent=None):
        super().__init__(parent)
        self.auth = auth
        self.setWindowTitle("Felhasználók és hozzáférési szerepkörök")
        self.resize(760, 420)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Felhasználónév", "Név", "Szerepkör", "Állapot"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.doubleClicked.connect(self._edit)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        for label, slot in (("Új felhasználó", self._new), ("Módosítás", self._edit)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            actions.addWidget(button)
        close = QPushButton("Bezárás")
        close.clicked.connect(self.accept)
        actions.addWidget(close)
        layout.addLayout(actions)
        self._reload()

    def _reload(self):
        try:
            self.users = self.auth.list_users()
        except Exception as error:
            QMessageBox.warning(self, "Felhasználók", str(error))
            self.reject()
            return
        self.table.setRowCount(len(self.users))
        for row, user in enumerate(self.users):
            values = (
                user["username"], user["display_name"], ROLE_LABELS[user["role"]],
                "Aktív" if user["active"] else "Letiltva",
            )
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.table.resizeColumnsToContents()

    def _new(self):
        if _UserDialog(self.auth, parent=self).exec() == QDialog.Accepted:
            self._reload()

    def _edit(self):
        row = self.table.currentRow()
        if 0 <= row < len(self.users):
            if _UserDialog(self.auth, self.users[row], self).exec() == QDialog.Accepted:
                self._reload()
