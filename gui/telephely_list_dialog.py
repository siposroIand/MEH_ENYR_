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
      - db: az alkalmazás Database objektuma (várja, hogy legyen get_telephelyek() és get_connection())
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
        except Exception:
            logger.exception("get_telephelyek failed")
            # lehetőség: üzenet a felhasználónak, de itt csak üres lista jön ha hiba.
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
            with self.db.get_connection() as conn:
                conn.execute("INSERT INTO telephelyek (id, nev, ktj, cim) VALUES (?,?,?,?)",
                             (new_id, d["nev"], d["ktj"], d["cim"]))
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
        try:
            with self.db.get_connection() as conn:
                conn.execute("UPDATE telephelyek SET nev=?, ktj=?, cim=? WHERE id=?",
                             (d["nev"], d["ktj"], d["cim"], t["id"]))
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
        # ellenőrzés: vannak-e engedélyek a telephelyhez (nem törölt állapotban)
        try:
            with self.db.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(1) as c FROM engedelyek WHERE telephely_id=? AND (deleted IS NULL OR deleted=0)", (t["id"],))
                row = cur.fetchone()
                if row and row["c"] > 0:
                    QMessageBox.warning(self, "Nem törölhető", "A telephelyhez engedélyek tartoznak — előbb törölje vagy áthelyezze azokat.")
                    return
        except Exception:
            # ha ellenőrzés nem sikerült, ne engedjük automatikusan törölni, kérjünk megerősítést
            pass

        if QMessageBox.question(self, "Törlés megerősítése", f"Tényleg törli a telephelyet?\n{t.get('nev')}") != QMessageBox.Yes:
            return

        try:
            with self.db.get_connection() as conn:
                conn.execute("DELETE FROM telephelyek WHERE id=?", (t["id"],))
            self._safe_log("telephely_delete")
        except Exception as e:
            logger.exception("Telephely törlése sikertelen: %s", e)
            QMessageBox.critical(self, "Hiba", f"Telephely törlése sikertelen: {e}")
            return

        self.refresh_list()