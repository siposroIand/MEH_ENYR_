import sys
import logging
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox, QDialog, QProgressDialog
from PySide6.QtGui import QIcon
from PySide6.QtCore import QObject, QTimer, Qt

from config.app_config import NETWORK_ROOT, DB_FILENAME, FAVICON_FILENAME, LOGO_FILENAME
from config.paths import build_app_dirs, get_resource_path
from config.settings import LOG_LEVEL
from core.db import Database
from core.services import PermitService
from gui.auth_dialogs import LoginDialog
from gui.workers import run_task, wait_for_tasks
from gui.app_ui import install_global_styles, set_windows_app_id
from gui.ui_texts import (
    APP_TITLE,
    MSG_DB_INIT_FAILED_TITLE,
    MSG_DB_INIT_FAILED_BODY,
)


def _setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    root.setLevel(level)
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)


class ApplicationController(QObject):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.window = None
        self.service = None
        self.exit_code = 0
        self.progress = QProgressDialog("Adatok és migrációk ellenőrzése…", "", 0, 0)
        self.progress.setWindowTitle(APP_TITLE)
        self.progress.setCancelButton(None)
        self.progress.setWindowFlag(Qt.WindowCloseButtonHint, False)

    def start(self):
        self.progress.show()

        def initialize():
            dirs = build_app_dirs(NETWORK_ROOT)
            db = Database(Path(dirs["db"]) / DB_FILENAME, backup_dir=dirs["backup"])
            try:
                service = PermitService(db, attachments_root=dirs["attachments"])
            except Exception:
                if hasattr(db, "close"):
                    db.close()
                raise
            return dirs, db, service

        run_task(self, initialize, self._ready, self._failed)

    def _failed(self, error):
        self.progress.close()
        QMessageBox.critical(None, MSG_DB_INIT_FAILED_TITLE,
                             f"{MSG_DB_INIT_FAILED_BODY}\n\nRészlet: {error}")
        self.exit_code = 1
        self.app.quit()

    def _ready(self, result):
        self.progress.close()
        self.dirs, self.db, self.service = result
        self.login()

    def login(self):
        try:
            dialog = LoginDialog(self.service)
        except Exception as error:
            self._failed(error)
            return
        if dialog.exec() != QDialog.Accepted:
            self.app.quit()
            return
        from gui.main_window import MainWindow

        try:
            self.window = MainWindow(
                db=self.db, dirs=self.dirs, logo_path=get_resource_path(LOGO_FILENAME),
                service=self.service,
            )
        except Exception as error:
            self._failed(error)
            return
        self.window.logout_requested.connect(self._logged_out)
        self.window.showMaximized()

    def _logged_out(self):
        old = self.window
        self.window = None
        if old is not None:
            old.deleteLater()
        QTimer.singleShot(0, self.login)

    def shutdown(self):
        wait_for_tasks()
        if self.service is not None:
            try:
                self.service.logout()
            except Exception:
                self.service.session = None
        if hasattr(self, "db") and hasattr(self.db, "close"):
            self.db.close()


def run_app() -> int:
    _setup_logging()
    set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(False)
    install_global_styles(app)
    favicon_path = get_resource_path(FAVICON_FILENAME)
    if favicon_path.exists():
        app.setWindowIcon(QIcon(str(favicon_path)))
    controller = ApplicationController(app)
    app.aboutToQuit.connect(controller.shutdown)
    QTimer.singleShot(0, controller.start)
    result = app.exec()
    return controller.exit_code or result


if __name__ == "__main__":
    raise SystemExit(run_app())