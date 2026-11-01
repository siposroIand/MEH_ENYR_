import logging
from uuid import uuid4
from typing import List

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QPushButton,
    QListWidgetItem,
    QMessageBox,
)
from PySide6.QtCore import Qt

from .telephely_dialog import TelephelyDialog



logger = logging.getLogger("engedely_nyilvantarto")


class TelephelyListDialog(QDialog):
    """
    Dialog telephelyek kezelésére.
    Konstruktor: TelephelyListDialog(db, parent=None)
      - db: helyi vagy központi adatkezelő a telephelyműveletekhez.
    """

    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Telephelyek kezelése")
        self.resize(560, 420)

        layout = QVBoxLayout(self)

        # lista
        self.listw = QListWidget()
        self.listw.setSelectionMode(QListWidget.SingleSelection)
        layout.addWidget(self.listw)

        # gombok
        btns = QHBoxLayout()
        self.btn_new = QPushButton("Hozzáadás")
        self.btn_edit = QPushButton("Módosítás")
        self.btn_delete = QPushButton("Törlés")
        self.btn_close = QPushButton("Bezár")

        btns.addWidget(self.btn_new)
        btns.addWidget(self.btn_edit)
        btns.addWidget(self.btn_delete)
        btns.addStretch()
        btns.addWidget(self.btn_close)
        layout.addLayout(btns)

        # csatlakoztatás
        self.btn_new.clicked.connect(self.new_telephely)
        self.btn_edit.clicked.connect(self.edit_selected)
        self.btn_delete.clicked.connect(self.delete_selected)
        self.btn_close.clicked.connect(self.accept)

        # feltöltés
        self.refresh_list()

    def refresh_list(self):
        """Betölti a telephelyeket a DB-ből, szűr a kereső alapján, és megjeleníti a listában."""
        self.listw.clear()
        tps: List[dict] = []
        try:
            tps = self.db.get_telephelyek()
        except Exception as exc:
            logger.exception("get_telephelyek failed")
            QMessageBox.warning(self, "Telephelyek", f"A lista nem tölthető be: {exc}")
        for t in sorted(tps, key=lambda x: (x.get("nev") or "").lower()):
            name = t.get("nev", "Ismeretlen")
            ktj = t.get("ktj")
            # Megjelenítés: Telephely (KTJ) ha KTJ van
            display = f"{name} | KTJ: {ktj}" if ktj else name

            item = QListWidgetItem(display)
            item.setData(Qt.UserRole, t)
            self.listw.addItem(item)

    def _safe_log(self, note: str):
        """Próbálja meghívni a db.log_event-et ha van (nem kötelező)."""
        fn = getattr(self.db, "log_event", None)
        if callable(fn):
            try:
                fn(note)
            except Exception:
                logger.debug("telephely list dialog: log_event failed", exc_info=True)

    def new_telephely(self):
        """Új telephely létrehozása dialógussal és DB-be beszúrás."""
        dlg = TelephelyDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        d = dlg.get_data()
        new_id = str(uuid4())
        try:
            self.db.save_telephely(d, new_id)
            self._safe_log("telephely_insert")
        except Exception as e:
            logger.exception("Telephely létrehozása sikertelen: %s", e)
            QMessageBox.critical(self, "Hiba", f"Telephely létrehozása sikertelen: {e}")
            return
        self.refresh_list()

    def edit_selected(self):
        """Kiválasztott telephely szerkesztése."""
        sel = self.listw.currentItem()
        if not sel:
            QMessageBox.information(self, "Nincs kiválasztva", "Válasszon ki egy telephelyet a szerkesztéshez.")
            return
        t = sel.data(Qt.UserRole)
        dlg = TelephelyDialog(self, telephely=t)
        if dlg.exec() != QDialog.Accepted:
            return
        d = dlg.get_data()
        d["revision"] = t.get("revision", "")
        try:
            self.db.save_telephely(d, t["id"], updating=True)
            self._safe_log("telephely_update")
        except Exception as e:
            logger.exception("Telephely frissítése sikertelen: %s", e)
            QMessageBox.critical(self, "Hiba", f"Telephely frissítése sikertelen: {e}")
            return
        self.refresh_list()

    def delete_selected(self):
        """Kiválasztott telephely törlése (ellenőrzi, hogy vannak-e engedélyek hozzárendelve)."""
        sel = self.listw.currentItem()
        if not sel:
            QMessageBox.information(self, "Nincs kiválasztva", "Válasszon ki egy telephelyet a törléshez.")
            return
        t = sel.data(Qt.UserRole)
        if QMessageBox.question(self, "Törlés megerősítése", f"Tényleg törli a telephelyet?\n{t.get('nev')}") != QMessageBox.Yes:
            return

        try:
            self.db.delete_telephely(t["id"], t.get("revision", ""))
            self._safe_log("telephely_delete")
        except Exception as e:
            logger.exception("Telephely törlése sikertelen: %s", e)
            QMessageBox.critical(self, "Hiba", f"Telephely törlése sikertelen: {e}")
            return

        self.refresh_list()