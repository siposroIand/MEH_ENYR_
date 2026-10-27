from __future__ import annotations

import sys
import ctypes

from PySide6.QtWidgets import QApplication, QToolTip
from PySide6.QtGui import QFont

from config.app_config import APP_NAME


def set_windows_app_id() -> None:
    if sys.platform.startswith("win"):
        try:
            app_id = f"mycompany.{APP_NAME}.v1"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        except Exception:
            pass


def install_global_styles(app: QApplication) -> None:
    QToolTip.setFont(QFont("Segoe UI", 10))
    app.setStyle("Fusion")
    app.setStyleSheet(
        """
        QToolTip {
            background-color: #e0dada;
            color: #000000;
            border: 1px solid #000000;
            padding: 2px;
            font-size: 11pt;
        }
        """
    )