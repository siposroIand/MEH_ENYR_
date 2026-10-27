from typing import Optional
from PySide6.QtWidgets import QMessageBox, QWidget
import gui.ui_texts as texts


def confirm(parent: Optional[QWidget], title: str, text: str, detailed_text: Optional[str] = None) -> bool:
    dlg = QMessageBox(parent)
    dlg.setWindowTitle(title or "")
    dlg.setText(text)
    if detailed_text:
        dlg.setInformativeText(detailed_text)
    yes = dlg.addButton(texts.BTN_YES, QMessageBox.AcceptRole)
    no = dlg.addButton(texts.BTN_NO, QMessageBox.RejectRole)
    dlg.setDefaultButton(no)
    dlg.setIcon(QMessageBox.Question)
    dlg.exec()
    return dlg.clickedButton() is yes


def info(parent: Optional[QWidget], title: str, text: str) -> None:
    dlg = QMessageBox(parent)
    dlg.setWindowTitle(title or "")
    dlg.setText(text)
    dlg.addButton(texts.BTN_OK, QMessageBox.AcceptRole)
    dlg.setIcon(QMessageBox.Information)
    dlg.exec()


def warning(parent: Optional[QWidget], title: str, text: str) -> None:
    dlg = QMessageBox(parent)
    dlg.setWindowTitle(title or "")
    dlg.setText(text)
    dlg.addButton(texts.BTN_OK, QMessageBox.AcceptRole)
    dlg.setIcon(QMessageBox.Warning)
    dlg.exec()


def error(parent: Optional[QWidget], title: str, text: str) -> None:
    dlg = QMessageBox(parent)
    dlg.setWindowTitle(title or "")
    dlg.setText(text)
    dlg.addButton(texts.BTN_OK, QMessageBox.AcceptRole)
    dlg.setIcon(QMessageBox.Critical)
    dlg.exec()