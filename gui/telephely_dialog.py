#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TelephelyDialog - telephely hozzáadás / szerkesztés dialógus
"""
import logging
from uuid import uuid4
from typing import Optional

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit, QPushButton, QHBoxLayout,
    QMessageBox
)
from PySide6.QtCore import Qt

logger = logging.getLogger("engedely_nyilvantarto")


class TelephelyDialog(QDialog):
    """
    Dialog a telephely (helyszín) adatainak bevitelére / szerkesztésére.
    usage:
        dlg = TelephelyDialog(parent, telephely=existing_dict_or_None)
        if dlg.exec() == QDialog.Accepted:
            data = dlg.get_data()
    """

    def __init__(self, parent=None, telephely: Optional[dict] = None):
        super().__init__(parent)
        self.setWindowTitle("Telephely adatok")
        self.resize(520, 220)
        self.telephely = telephely or {}

        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        self.name_inp = QLineEdit(self.telephely.get("nev", ""))
        self.name_inp.setPlaceholderText("Telephely megnevezése (kötelező)")
        self.name_inp.setMinimumWidth(300)

        self.ktj_inp = QLineEdit(self.telephely.get("ktj", ""))
        self.ktj_inp.setPlaceholderText("KTJ szám (opcionális)")

        self.cim_inp = QLineEdit(self.telephely.get("cim", ""))
        self.cim_inp.setPlaceholderText("Cím (opcionális)")

        form.addRow("Telephely neve*", self.name_inp)
        form.addRow("KTJ szám", self.ktj_inp)
        form.addRow("Cím", self.cim_inp)

        layout.addLayout(form)

        # buttons
        btns = QHBoxLayout()
        btns.addStretch()
        ok = QPushButton("Mentés")
        cancel = QPushButton("Mégse")
        ok.clicked.connect(self._on_ok)
        cancel.clicked.connect(self.reject)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        layout.addLayout(btns)

    def _on_ok(self):
        """Validálás: név mező kötelező."""
        if not self.name_inp.text().strip():
            QMessageBox.warning(self, "Hiányzó adat", "A telephely neve kötelező.")
            return
        self.accept()

    def get_data(self) -> dict:
        """Visszaadja a megadott adatokat dict-ként (érték nélküli mezők None)."""
        return {
            "nev": self.name_inp.text().strip(),
            "ktj": self.ktj_inp.text().strip() or None,
            "cim": self.cim_inp.text().strip() or None
        }