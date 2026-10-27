"""Site management through the authenticated domain service."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QPushButton,
    QListWidgetItem, QMessageBox,
)

from .telephely_dialog import TelephelyDialog


class TelephelyListDialog(QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Telephelyek kezelése")
        self.resize(560, 420)
        layout = QVBoxLayout(self)
        self.listw = QListWidget()
        self.listw.setSelectionMode(QListWidget.SingleSelection)
        layout.addWidget(self.listw)
        buttons = QHBoxLayout()
        for name, title, slot in (
            ("btn_new", "Hozzáadás", self.new_telephely),
            ("btn_edit", "Módosítás", self.edit_selected),
            ("btn_delete", "Törlés", self.delete_selected),
            ("btn_close", "Bezár", self.accept),
        ):
            button = QPushButton(title)
            setattr(self, name, button)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.refresh_list()

    def refresh_list(self):
        selected = self.listw.currentItem()
        selected_id = selected.data(Qt.UserRole)["id"] if selected else None
        try:
            sites = self.service.snapshot()["telephelyek"]
        except Exception as exc:
            QMessageBox.critical(self, "Frissítés sikertelen", str(exc))
            return
        self.listw.clear()
        for site in sorted(sites, key=lambda row: (row.get("nev") or "").casefold()):
            text = site.get("nev") or "Ismeretlen"
            if site.get("ktj"):
                text += f" | KTJ: {site['ktj']}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, site)
            self.listw.addItem(item)
            if site["id"] == selected_id:
                self.listw.setCurrentItem(item)

    def _save(self, dialog, site=None):
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            values = dialog.get_data()
            if site:
                values["id"] = site["id"]
            self.service.save_site(values, expected_version=site["version"] if site else None)
        except Exception as exc:
            QMessageBox.critical(self, "Mentés sikertelen", str(exc))
            return
        self.refresh_list()

    def new_telephely(self):
        self._save(TelephelyDialog(self))

    def edit_selected(self):
        selected = self.listw.currentItem()
        if selected:
            site = selected.data(Qt.UserRole)
            self._save(TelephelyDialog(self, telephely=site), site)
        else:
            QMessageBox.information(self, "Nincs kiválasztva", "Válasszon ki egy telephelyet.")

    def delete_selected(self):
        selected = self.listw.currentItem()
        if not selected:
            QMessageBox.information(self, "Nincs kiválasztva", "Válasszon ki egy telephelyet.")
            return
        site = selected.data(Qt.UserRole)
        if QMessageBox.question(self, "Törlés", f"Tényleg törli a telephelyet?\n{site.get('nev')}") != QMessageBox.Yes:
            return
        try:
            self.service.delete_site(site["id"], expected_version=site["version"])
        except Exception as exc:
            QMessageBox.critical(self, "Törlés sikertelen", str(exc))
            return
        self.refresh_list()
