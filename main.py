import sys
import logging
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QIcon

from config.app_config import NETWORK_ROOT, DB_FILENAME, FAVICON_FILENAME, LOGO_FILENAME
from config.paths import build_app_dirs, get_resource_path
from config.settings import LOG_LEVEL
from core.db import Database
from gui.app_ui import install_global_styles, set_windows_app_id
from gui.ui_texts import (
    APP_TITLE,
    MSG_NETWORK_ROOT_UNAVAILABLE_TITLE,
    MSG_NETWORK_ROOT_UNAVAILABLE_BODY,
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


def run_app() -> int:
    _setup_logging()
    set_windows_app_id()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    install_global_styles(app)

    # Könyvtárak és DB
    try:
        dirs = build_app_dirs(NETWORK_ROOT)
    except Exception as e:
        QMessageBox.critical(
            None,
            MSG_NETWORK_ROOT_UNAVAILABLE_TITLE,
            f"{MSG_NETWORK_ROOT_UNAVAILABLE_BODY}\n\nRészlet: {e}",
        )
        return 1

    db_file = Path(dirs["db"]) / DB_FILENAME
    try:
        db = Database(db_file, backup_dir=dirs["backup"])
    except Exception as e:
        QMessageBox.critical(
            None,
            MSG_DB_INIT_FAILED_TITLE,
            f"{MSG_DB_INIT_FAILED_BODY}\n\nRészlet: {e}",
        )
        return 1

    # Ikonok
    favicon_path = get_resource_path(FAVICON_FILENAME)
    logo_path = get_resource_path(LOGO_FILENAME)

    if favicon_path.exists():
        app_icon = QIcon(str(favicon_path))
        app.setWindowIcon(app_icon)

    from gui.main_window import MainWindow

    win = MainWindow(db=db, dirs=dirs, logo_path=logo_path)
    if favicon_path.exists():
        win.setWindowIcon(QIcon(str(favicon_path)))

    win.showMaximized()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run_app())