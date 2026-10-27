#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Authenticated permit registry UI. All writes belong to the domain service."""
from __future__ import annotations

import csv
import copy
import logging
import os
import shlex
import subprocess
import sys
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QTabWidget,
    QToolButton, QVBoxLayout, QWidget, QWidgetAction,
)

from config.app_config import get_ordered_categories
from core.attachments import ALLOWED_EXTENSIONS
from exporters.excel_exporter import export_to_excel as export_excel_fn
from exporters.pdf_exporter import export_groups_pdf
from gui.engedely_dialog import EngedelyDialog
from gui.report_builder import ReportBuilderDialog, run_report
from gui.telephely_list_dialog import TelephelyListDialog
from gui.ui_helpers import confirm, error, info, warning
from gui.ui_texts import APP_TITLE, INFO_NO_ATTACHMENTS
from utils.fs_utils import sanitize_filename

logger = logging.getLogger("engedely_nyilvantarto")
FIX_KATEGORIAK = get_ordered_categories()


def _open_path(path):
    if sys.platform.startswith("win"):
        os.startfile(str(path))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


class AttachDropTable(QTableWidget):
    filesDropped = Signal(int, list)

    def __init__(self, rows=0, cols=0, parent=None):
        super().__init__(rows, cols, parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, event):
        row = self.rowAt(event.position().toPoint().y())
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if row >= 0 and paths:
            self.filesDropped.emit(row, paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class DropdownMultiSelect(QWidget):
    selectionChanged = Signal()

    def __init__(self, placeholder="(összes)", parent=None):
        super().__init__(parent)
        self._placeholder = placeholder
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.button = QToolButton(self)
        self.button.setPopupMode(QToolButton.InstantPopup)
        self.button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.menu = QMenu(self)
        self.listw = QListWidget()
        self.listw.setSelectionMode(QAbstractItemView.ExtendedSelection)
        action = QWidgetAction(self.menu)
        action.setDefaultWidget(self.listw)
        self.menu.addAction(action)
        self.menu.addAction("Kész", self.menu.hide)
        self.button.setMenu(self.menu)
        layout.addWidget(self.button)
        self.listw.itemSelectionChanged.connect(self._changed)
        self._update_display()

    def set_items(self, items):
        self.listw.blockSignals(True)
        try:
            self.listw.clear()
            for entry in items:
                text, value = entry[:2]
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, value)
                self.listw.addItem(item)
                item.setSelected(bool(entry[2]) if len(entry) > 2 else False)
        finally:
            self.listw.blockSignals(False)
        self._update_display()

    def checked_data(self):
        return [item.data(Qt.UserRole) for item in self.listw.selectedItems()]

    def checked_texts(self):
        return [item.text() for item in self.listw.selectedItems()]

    def set_checked_by_data(self, values):
        self.listw.blockSignals(True)
        for i in range(self.listw.count()):
            item = self.listw.item(i)
            item.setSelected(item.data(Qt.UserRole) in values)
        self.listw.blockSignals(False)
        self._changed()

    def _update_display(self):
        selected = self.checked_texts()
        self.button.setText(selected[0] if len(selected) == 1 else
                            f"{len(selected)} kiválasztva" if selected else self._placeholder)

    def _changed(self):
        self._update_display()
        self.selectionChanged.emit()


class BackupRestoreDialog(QDialog):
    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self.setWindowTitle("Biztonsági mentések")
        self.resize(700, 420)
        layout = QVBoxLayout(self)
        self.listw = QListWidget()
        layout.addWidget(self.listw)
        row = QHBoxLayout()
        self.btn_backup = QPushButton("Mentés készítése")
        self.btn_restore = QPushButton("Visszaállítás")
        self.btn_open = QPushButton("Mappa megnyitása")
        close = QPushButton("Bezár")
        for button in (self.btn_backup, self.btn_restore, self.btn_open, close):
            row.addWidget(button)
        layout.addLayout(row)
        self.btn_backup.clicked.connect(self._do_backup)
        self.btn_restore.clicked.connect(self._do_restore)
        self.btn_open.clicked.connect(self._do_open)
        close.clicked.connect(self.accept)
        self.refresh_list()

    def refresh_list(self):
        try:
            backups = self.service.list_backups()
        except Exception as exc:
            error(self, "Mentések", str(exc))
            return
        self.listw.clear()
        for path in backups:
            path = Path(path)
            item = QListWidgetItem(path.name)
            item.setData(Qt.UserRole, path)
            self.listw.addItem(item)

    def _do_backup(self):
        from gui.workers import run_task
        self.btn_backup.setEnabled(False)

        def done(path):
            self.btn_backup.setEnabled(True)
            if not self.isVisible():
                return
            info(self, "Mentés", f"Mentés elkészült: {path}")
            self.refresh_list()

        def failed(exc):
            self.btn_backup.setEnabled(True)
            if self.isVisible():
                error(self, "Mentés", str(exc))

        run_task(self, self.service.create_backup, done, failed)

    def _do_restore(self):
        selected = self.listw.currentItem()
        if selected and confirm(self, "Visszaállítás", "Visszaállítja a kiválasztott mentést?"):
            try:
                self.service.restore_backup(selected.data(Qt.UserRole))
                info(self, "Visszaállítás", "A visszaállítás elkészült. Indítsa újra az alkalmazást.")
            except Exception as exc:
                error(self, "Visszaállítás", str(exc))

    def _do_open(self):
        selected = self.listw.currentItem()
        if selected:
            try:
                path = selected.data(Qt.UserRole)
                if path not in [Path(item) for item in self.service.list_backups()]:
                    raise ValueError("A mentés már nem érhető el.")
                _open_path(path.parent)
            except Exception as exc:
                error(self, "Mentések", str(exc))


class ExpiryDialog(QDialog):
    def __init__(self, expiring_6m=None, expiring_3m=None, expired=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lejáró engedélyek")
        self.resize(900, 520)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        for title, records in (("6 hónap", expiring_6m), ("3 hónap", expiring_3m), ("Lejárt", expired)):
            listing = QListWidget()
            for record in records or []:
                listing.addItem(" | ".join(str(record.get(key) or "-") for key in
                                          ("lejarat", "telephely_nev", "kategoria", "engedely_nev", "hatarozatszam")))
            tabs.addTab(listing, f"{title} ({len(records or [])})")
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ReviewDialog(QDialog):
    def __init__(self, review_all=None, telephelyek=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Felülvizsgálatok")
        self.resize(1000, 620)
        names = {t["id"]: t["nev"] for t in telephelyek or []}
        self._entries = [{
            "date": day, "note": note, "telephely": names.get(record.get("telephely_id"), "Ismeretlen"),
            "kategoria": record.get("kategoria") or "", "engedely_nev": record.get("engedely_nev") or "",
            "hatarozatszam": record.get("hatarozatszam") or "",
        } for record, day, note in review_all or []]
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel(f"Felülvizsgálatok: {len(self._entries)}"))
        self.group_combo = QComboBox()
        self.group_combo.addItems(["Telephely szerint", "Engedély szerint", "Lista (összes)"])
        self.group_combo.currentIndexChanged.connect(self._rebuild_view)
        row.addWidget(self.group_combo)
        export = QPushButton("Exportálás Excel")
        export.clicked.connect(self._export_excel)
        row.addWidget(export)
        layout.addLayout(row)
        self.listw = QListWidget()
        layout.addWidget(self.listw)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._rebuild_view()

    def _rebuild_view(self):
        self.listw.clear()
        mode = self.group_combo.currentIndex()
        key = "telephely" if mode == 0 else "engedely_nev"
        entries = sorted(self._entries, key=lambda e: (e[key] if mode < 2 else "", e["date"]))
        previous = None
        for entry in entries:
            group = entry[key]
            if mode < 2 and group != previous:
                header = QListWidgetItem(group)
                font = QFont()
                font.setBold(True)
                header.setFont(font)
                self.listw.addItem(header)
                previous = group
            self.listw.addItem(f"{entry['date'].isoformat()} | {entry['telephely']} | "
                               f"{entry['kategoria']} | {entry['engedely_nev']} — "
                               f"{entry['hatarozatszam']} | {entry['note']}")

    def _export_excel(self):
        path, _ = QFileDialog.getSaveFileName(self, "Exportálás Excel",
                                             f"felulvizsgalatok_{date.today():%Y%m%d}.xlsx",
                                             "Excel (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        rows = [["Dátum", "Telephely", "Engedély", "Felülvizsgálat tárgya"]]
        rows.extend([e["date"].isoformat(), e["telephely"], e["engedely_nev"], e["note"]]
                    for e in sorted(self._entries, key=lambda e: e["date"]))
        try:
            if Path(path).suffix.lower() == ".csv":
                with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                    csv.writer(stream, delimiter=";").writerows(
                        [self._csv_text(value) for value in row] for row in rows)
            else:
                from openpyxl import Workbook
                workbook = Workbook()
                worksheet = workbook.active
                for row in rows:
                    worksheet.append(row)
                    for cell in worksheet[worksheet.max_row]:
                        if isinstance(cell.value, str):
                            cell.data_type = "s"
                workbook.save(str(Path(path).with_suffix(".xlsx")))
            info(self, "Export", "Sikeres exportálás.")
        except Exception as exc:
            error(self, "Export", str(exc))

    @staticmethod
    def _csv_text(value):
        text = str(value)
        return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


class MainWindow(QMainWindow):
    logout_requested = Signal()

    def __init__(self, db, dirs, logo_path, service):
        super().__init__()
        self.service = service
        self.auth = service.auth
        self.db = db
        self.dirs = dirs or {}
        self.logo_path = Path(logo_path) if logo_path else Path.cwd() / "logo.png"
        self.ATTACHMENTS_DIR = Path(self.dirs.get("attachments", Path.cwd() / "attachment"))
        self.data = {"telephelyek": [], "engedelyek": [], "users": []}
        self._user = {}
        self._attachments_cache = {}
        self._attachment_count_generation = 0
        self._attachment_counts_running = False
        self._pending_attachment_counts = None
        self._snapshot_generation = 0
        self._snapshot_refresh_running = False
        self._pending_snapshot_refresh = None
        self._logged_out = False
        self._closing = False
        self._backup_running = False
        self._export_running = False
        self._last_export_options = {}
        initial = self.service.snapshot()  # A window must never exist without a valid session.
        self.setWindowTitle(APP_TITLE)
        self.resize(1400, 760)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        account_row = QHBoxLayout()
        self.profile_label = QLabel()
        account_row.addWidget(self.profile_label)
        account_row.addStretch()
        self.btn_profile = QPushButton("Profil")
        self.btn_password = QPushButton("Jelszó módosítása")
        self.btn_users = QPushButton("Felhasználók")
        self.btn_logout = QPushButton("Kijelentkezés")
        for button in (self.btn_profile, self.btn_password, self.btn_users, self.btn_logout):
            account_row.addWidget(button)
        self.btn_profile.clicked.connect(self.open_profile)
        self.btn_password.clicked.connect(self.open_profile)
        self.btn_users.clicked.connect(self.open_users)
        self.btn_logout.clicked.connect(self.logout)
        layout.addLayout(account_row)

        filters = QHBoxLayout()
        self.kategoria_filter = DropdownMultiSelect("(összes engedélytípus)")
        self.kategoria_filter.set_items([(name, name) for name in FIX_KATEGORIAK])
        self.telephely_filter = DropdownMultiSelect("(összes telephely)")
        self.responsibility_filter = QComboBox()
        self.responsibility_filter.addItem("Összes felelős", "all")
        self.responsibility_filter.addItem("Saját engedélyeim", "own")
        self.responsibility_filter.addItem("Nincs felelős", "unassigned")
        self.show_archived = QCheckBox("Archivált adatok")
        self.global_search = QLineEdit()
        self.global_search.setPlaceholderText("Kifejezés keresése")
        for widget in (self.kategoria_filter, self.telephely_filter, self.responsibility_filter,
                       self.show_archived, self.global_search):
            filters.addWidget(widget)
        layout.addLayout(filters)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self.refresh_table)
        self.global_search.textChanged.connect(lambda _: self._search_timer.start())
        self.kategoria_filter.selectionChanged.connect(self.refresh_table)
        self.telephely_filter.selectionChanged.connect(self.refresh_table)
        self.responsibility_filter.currentIndexChanged.connect(self.refresh_table)
        self.show_archived.stateChanged.connect(self.refresh_table)

        self.table = AttachDropTable(0, 10)
        self.table.setHorizontalHeaderLabels([
            "Telephely", "Engedélytípus", "Tárgy", "Határozatszám", "Módosító",
            "Hatálya", "Felülvizsgálat", "Megjegyzés", "Engedély(ek)", "Felelős",
        ])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        self.table.setColumnWidth(2, 320)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Interactive)
        header.setSectionResizeMode(5, QHeaderView.Fixed)
        self.table.setColumnWidth(5, 78)
        header.setSectionResizeMode(6, QHeaderView.Fixed)
        self.table.setColumnWidth(6, 97)
        header.setSectionResizeMode(7, QHeaderView.Stretch)
        header.setSectionResizeMode(8, QHeaderView.Fixed)
        self.table.setColumnWidth(8, 105)
        header.setSectionResizeMode(9, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._on_table_double_click)
        self.table.filesDropped.connect(self._on_files_dropped)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        layout.addWidget(self.table)

        controls = QHBoxLayout()
        groups = (
            ("Adminisztráció", (
                ("btn_tel", "Telephelyek", self.open_telephely_manager),
                ("btn_eng", "Engedély rögzítés", self.add_engedely),
                ("btn_assign", "Felelős kijelölése", self.assign_selected),
                ("btn_show_expiring", "⚠️ Lejáró engedélyek", self.show_expiring_dialog),
                ("btn_reviews", "📝 Felülvizsgálatok", self.show_reviews_dialog),
            )),
            ("Exportálás", (
                ("btn_quick_tp", "🏭 Telephelyenkénti export", lambda: self.batch_export("telephely")),
                ("btn_quick_kat", "📄 Engedély szerinti export", lambda: self.batch_export("kategoria")),
                ("btn_excel", "📊 Excel Export", self.export_to_excel),
                ("btn_view_export", "📊 Kimutatáskészítő", self.open_report_builder),
            )),
            ("Rendszer / Mentések", (
                ("btn_refresh", "🔄 Frissítés", self.manual_refresh),
                ("btn_backup_now", "⤓ Mentés készítése", self.create_backup_now),
                ("btn_backup_list", "🔒 Mentések", self.open_backup_dialog),
            )),
        )
        for title, definitions in groups:
            group = QGroupBox(title)
            row = QHBoxLayout(group)
            row.setContentsMargins(8, 6, 8, 6)
            for name, text, slot in definitions:
                button = QPushButton(text)
                setattr(self, name, button)
                button.clicked.connect(slot)
                row.addWidget(button)
            controls.addWidget(group)
            if title != "Rendszer / Mentések":
                controls.addSpacing(10)
            if title == "Exportálás":
                controls.addStretch()
        layout.addLayout(controls)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.silent_refresh)
        self.timer.start(300000)
        for key, slot in (("Ctrl+B", self.create_backup_now), ("Ctrl+P", self.open_report_builder),
                          ("Ctrl+N", self.add_engedely), ("Ctrl+R", self.manual_refresh),
                          ("F5", self.manual_refresh), ("Escape", self._close_active_popup)):
            QShortcut(QKeySequence(key), self).activated.connect(slot)
        delete = QShortcut(QKeySequence(Qt.Key_Delete), self.table)
        delete.setContext(Qt.WidgetWithChildrenShortcut)
        delete.activated.connect(self._delete_selected_via_shortcut)
        self._apply_snapshot(initial)

    def _current_user(self):
        return self._user

    def _is_admin(self):
        return self._current_user().get("role") == "admin"

    def _can_write(self, record=None):
        user = self._current_user()
        return user.get("role") == "admin" or (
            user.get("role") == "clerk" and
            (record is None or record.get("responsible_user_id") == user.get("id")))

    def _update_profile(self):
        user = self._current_user()
        from core.auth import ROLE_LABELS
        self.profile_label.setText(f"{user.get('display_name') or user.get('username', '')} "
                                   f"({ROLE_LABELS.get(user.get('role'), '')})")
        for widget in (self.btn_users, self.btn_assign, self.btn_tel, self.btn_backup_list, self.btn_backup_now):
            widget.setVisible(self._is_admin())
        self.btn_eng.setEnabled(user.get("role") in ("admin", "clerk"))

    def open_profile(self):
        from core.auth import AuthenticationError
        from gui.auth_dialogs import ProfileDialog
        try:
            ProfileDialog(self.auth, self).exec()
        except AuthenticationError:
            self.logout()
            return
        except Exception as exc:
            error(self, "Profil", str(exc))
            return
        if self.auth.session is None:
            self.logout()
        else:
            self.manual_refresh()

    def open_users(self):
        from gui.auth_dialogs import UsersDialog
        if self._is_admin():
            UsersDialog(self.auth, self).exec()
            self.manual_refresh()

    def _clear_session_ui(self):
        self._snapshot_generation += 1
        self._pending_snapshot_refresh = None
        self._attachment_count_generation += 1
        self._pending_attachment_counts = None
        self.timer.stop()
        self._search_timer.stop()
        for dialog in self.findChildren(QDialog):
            dialog.reject()
            dialog.close()
            dialog.hide()  # Busy dialogs may defer closing until their worker cleans staging.
        self._attachments_cache.clear()
        self.data = {"telephelyek": [], "engedelyek": [], "users": []}
        self.table.setRowCount(0)
        self.profile_label.clear()
        self.global_search.clear()
        self.telephely_filter.set_items([])
        self.kategoria_filter.set_checked_by_data([])
        self.responsibility_filter.blockSignals(True)
        self.responsibility_filter.clear()
        self.responsibility_filter.blockSignals(False)
        self._last_export_options = {}
        self._user = {}
        self._search_timer.stop()

    def logout(self):
        if self._logged_out:
            return
        self._logged_out = True
        self._clear_session_ui()
        try:
            self.auth.logout()
        except Exception:
            logger.exception("Session revocation failed during logout")
        finally:
            self.auth.session = None
            self.close()
            self.logout_requested.emit()

    def closeEvent(self, event):
        self._closing = True
        if hasattr(self, "timer"):
            self._clear_session_ui()
        if not self._logged_out:
            try:
                self.auth.logout()
            except Exception:
                logger.exception("Session revocation failed during close")
            finally:
                self.auth.session = None
        event.accept()
        if not self._logged_out:
            app = QApplication.instance()
            if app is not None:
                app.quit()

    def _apply_snapshot(self, snapshot):
        from utils.reviews import review_entries, review_occurrences, next_review_date, compute_review_date
        from utils.dates import parse_iso_date
        candidate = {key: [dict(row) for row in snapshot.get(key, [])]
                     for key in ("telephelyek", "engedelyek", "users")}
        names = {site["id"]: site.get("nev", "") for site in candidate["telephelyek"]}
        users = {user["id"]: user.get("display_name") or user.get("username", "")
                 for user in candidate["users"]}
        for record in candidate["engedelyek"]:
            record["_lej_dt"] = parse_iso_date(record.get("lejarat"))
            record["_fv_list"] = review_entries(record)
            record["_next_fv"] = next_review_date(record) or compute_review_date(record)
            occurrences = review_occurrences(record)
            record["_next_fv_computed"] = any(
                day == record["_next_fv"] and entry["type"] == "before_expiry"
                for day, entry in occurrences)
            record["_fv_tooltip"] = "\n".join(
                f"{day.isoformat()}: {entry.get('note', '')}" +
                (f" ({entry['days']} nappal a lejárat előtt, számított)" if entry["type"] == "before_expiry" else "")
                for day, entry in occurrences)
            assignee = record.get("responsible_user_id")
            record["_responsible_name"] = users.get(
                assignee, "Ismeretlen felelős" if assignee else "Nincs felelős")
            record["responsible_name"] = record["_responsible_name"]
            fields = [record.get(key) or "" for key in (
                "engedely_nev", "hatarozatszam", "modosito_hat", "kiado_hatosag",
                "megjegyzes", "kategoria", "_responsible_name")]
            record["_search_index"] = " ".join(map(str, fields + [names.get(record.get("telephely_id"), "")])).casefold()
        user = dict(snapshot.get("current_user") or self.auth.current_user)
        self.data = candidate
        self._user = user
        self._attachments_cache.clear()
        self._attachment_count_generation += 1
        self.refresh_telephely_filter()
        self.refresh_responsibility_filter()
        self._update_profile()
        self.refresh_table()
        self._queue_attachment_counts(
            self._attachment_count_generation, tuple(record["id"] for record in candidate["engedelyek"]))

    def refresh_data(self, incremental=False):
        """Synchronous compatibility API; user/timer refreshes use the worker."""
        if self._logged_out or self._closing:
            return False
        self._snapshot_generation += 1
        try:
            snapshot = self.service.snapshot()
            self._apply_snapshot(snapshot)
            self.statusBar().clearMessage()
            return True
        except Exception as exc:
            self._snapshot_failed(exc)
            return False

    def manual_refresh(self):
        if self._logged_out or self._closing:
            return
        self._snapshot_generation += 1
        generation = self._snapshot_generation
        if self._snapshot_refresh_running:
            self._pending_snapshot_refresh = generation
            return
        self._start_snapshot_refresh(generation)

    def _start_snapshot_refresh(self, generation):
        from gui.workers import run_task
        self._snapshot_refresh_running = True
        self.statusBar().showMessage("Frissítés folyamatban…")
        service = self.service

        def done(snapshot):
            self._finish_snapshot_refresh(generation, snapshot, None)

        def failed(exc):
            self._finish_snapshot_refresh(generation, None, exc)

        run_task(self, service.snapshot, done, failed)

    def _finish_snapshot_refresh(self, generation, snapshot, exc):
        self._snapshot_refresh_running = False
        if self._logged_out or self._closing:
            self._pending_snapshot_refresh = None
            return
        if generation == self._snapshot_generation:
            if exc is not None:
                self._snapshot_failed(exc)
            else:
                try:
                    self._apply_snapshot(snapshot)
                    self.statusBar().clearMessage()
                except Exception as failure:
                    self._snapshot_failed(failure)
        if self._logged_out or self._closing:
            return
        pending = self._pending_snapshot_refresh
        self._pending_snapshot_refresh = None
        if pending is not None and pending == self._snapshot_generation:
            self._start_snapshot_refresh(pending)

    def _snapshot_failed(self, exc):
        from core.auth import AuthenticationError
        if isinstance(exc, AuthenticationError):
            self.logout()
            return
        logger.error("Snapshot refresh failed: %s", exc)
        self.statusBar().showMessage(f"Frissítés sikertelen, az utolsó sikeres nézet látható: {exc}")

    def silent_refresh(self):
        if not self.global_search.hasFocus():
            self.manual_refresh()

    def refresh_telephely_filter(self):
        selected = self.telephely_filter.checked_data()
        self.telephely_filter.set_items([
            (site.get("nev", "Ismeretlen"), site["id"], site["id"] in selected)
            for site in self.data["telephelyek"]])

    def refresh_responsibility_filter(self):
        selected = self.responsibility_filter.currentData()
        selected_text = self.responsibility_filter.currentText()
        self.responsibility_filter.blockSignals(True)
        try:
            self.responsibility_filter.clear()
            for label, value in (("Összes felelős", "all"), ("Saját engedélyeim", "own"),
                                 ("Nincs felelős", "unassigned")):
                self.responsibility_filter.addItem(label, value)
            users = sorted(self.data["users"],
                           key=lambda user: (user.get("display_name") or user.get("username") or "").casefold())
            for user in users:
                name = user.get("display_name") or user.get("username") or "Ismeretlen felhasználó"
                if not user.get("active", 1):
                    name += " (inaktív)"
                self.responsibility_filter.addItem(f"Személy: {name}", f"person:{user['id']}")
            index = self.responsibility_filter.findData(selected)
            if index < 0 and isinstance(selected, str) and selected.startswith("person:"):
                self.responsibility_filter.addItem(selected_text, selected)
                index = self.responsibility_filter.count() - 1
            self.responsibility_filter.setCurrentIndex(max(index, 0))
        finally:
            self.responsibility_filter.blockSignals(False)

    def _get_filtered_items(self):
        items = [e for e in self.data["engedelyek"] if not e.get("deleted")]
        search = self.global_search.text().strip()
        try:
            tokens = shlex.split(search)
        except ValueError:
            tokens = search.split()
        categories = self.kategoria_filter.checked_data()
        sites = self.telephely_filter.checked_data()
        responsibility = self.responsibility_filter.currentData()
        person_id = responsibility.removeprefix("person:") if (
            isinstance(responsibility, str) and responsibility.startswith("person:")) else None
        return [record for record in items
                if (not categories or record.get("kategoria") in categories)
                and (not sites or record.get("telephely_id") in sites)
                and (self.show_archived.isChecked() or not record.get("archivalt"))
                and all(token.casefold() in record.get("_search_index", "") for token in tokens)
                and (responsibility != "own" or record.get("responsible_user_id") == self._current_user().get("id"))
                and (responsibility != "unassigned" or not record.get("responsible_user_id"))
                and (person_id is None or record.get("responsible_user_id") == person_id)]

    def _get_selected_entities(self):
        return [self.table.item(index.row(), 0).data(Qt.UserRole)
                for index in self.table.selectionModel().selectedRows()
                if self.table.item(index.row(), 0)]

    def _attachments_count(self, record):
        return self._attachments_cache.get(record["id"])

    def _queue_attachment_counts(self, generation, identifiers):
        if self._closing or self._logged_out or not identifiers:
            return
        if self._attachment_counts_running:
            self._pending_attachment_counts = (generation, identifiers)
            return
        from gui.workers import run_task
        self._attachment_counts_running = True
        service = self.service
        batch_count = getattr(service, "count_attachments", None)

        def count():
            if batch_count is not None:
                return batch_count(identifiers)
            counts = {}
            for identifier in identifiers:
                plan = service.attachment_plan(identifier, readonly=True)
                try:
                    counts[identifier] = len(plan.list_names())
                finally:
                    plan.cleanup()
            return counts

        def done(counts):
            self._finish_attachment_counts(generation, counts, None)

        def failed(exc):
            self._finish_attachment_counts(generation, None, exc)

        run_task(self, count, done, failed)

    def _finish_attachment_counts(self, generation, counts, exc):
        self._attachment_counts_running = False
        if self._closing or self._logged_out:
            self._pending_attachment_counts = None
            return
        if generation == self._attachment_count_generation:
            if exc is None:
                self._attachments_cache = dict(counts)
                self._update_attachment_cells()
            else:
                from core.auth import AuthenticationError
                if isinstance(exc, AuthenticationError):
                    self.logout()
                    return
                self.statusBar().showMessage(f"A csatolmányok számlálása sikertelen: {exc}")
        pending = self._pending_attachment_counts
        self._pending_attachment_counts = None
        if pending is not None and pending[0] == self._attachment_count_generation:
            self._queue_attachment_counts(*pending)

    @staticmethod
    def _attachment_text(count):
        return "Csatolmányok…" if count is None else f"Megnyitás ({count})" if count else "Nincs csatolmány"

    def _update_attachment_cells(self):
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        try:
            for row in range(self.table.rowCount()):
                record = self.table.item(row, 0).data(Qt.UserRole)
                item = self.table.item(row, 8)
                text = self._attachment_text(self._attachments_count(record))
                item.setText(text)
                item.setToolTip(text)
        finally:
            self.table.setSortingEnabled(sorting)

    def refresh_table(self):
        if not hasattr(self, "table"):
            return
        selected = {e["id"] for e in self._get_selected_entities()}
        header = self.table.horizontalHeader()
        sort_column, sort_order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        sites = {site["id"]: site.get("nev", "") for site in self.data["telephelyek"]}
        items = self._get_filtered_items()
        self.table.setRowCount(len(items))
        today = date.today()
        try:
            for row, record in enumerate(items):
                validity = record.get("hataly_tipus") or ("indefinite" if record.get("hatarozatlan") else "fixed")
                validity_text = record.get("lejarat") or "-" if validity == "fixed" else (
                    "Határozatlan" if validity == "indefinite" else "Változtatásig")
                reviews = record.get("_fv_list", [])
                next_review = record.get("_next_fv")
                count = self._attachments_count(record)
                waste = ", ".join(f"{label}: {record[key]}" for key, label in (
                    ("hulladek_nv_elokez", "NV-Elő"), ("hulladek_v_elokez", "V-Elő"),
                    ("hulladek_nv_gyujt", "NV-Gyűjt"), ("hulladek_v_gyujt", "V-Gyűjt"))
                    if record.get(key))
                values = [
                    sites.get(record.get("telephely_id"), "Ismeretlen"), record.get("kategoria"),
                    record.get("engedely_nev"), record.get("hatarozatszam"), record.get("modosito_hat"),
                    validity_text, (f"{next_review.isoformat()} ({len(reviews)})" +
                                    (" *" if record.get("_next_fv_computed") else "")) if next_review else "-",
                    (record.get("megjegyzes") or "") + (f" [Hulladék: {waste}]" if waste else ""),
                    self._attachment_text(count), record.get("_responsible_name"),
                ]
                color = QColor("#ffffff")
                deadline = record.get("_lej_dt") if validity == "fixed" else None
                deadline = deadline or next_review
                if record.get("archivalt"):
                    color = QColor("#d1d0d0")
                elif deadline:
                    days = (deadline - today).days
                    color = QColor("#ff8383" if days < 0 else "#f9c2a0" if days <= 90 else
                                   "#fff173" if days <= 180 else "#ffffff")
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value or ""))
                    item.setBackground(color)
                    if record.get("archivalt"):
                        item.setForeground(QColor("#424242"))
                    item.setToolTip(str(value or ""))
                    self.table.setItem(row, column, item)
                self.table.item(row, 0).setData(Qt.UserRole, record)
                self.table.item(row, 6).setToolTip(record.get("_fv_tooltip", ""))
        finally:
            self.table.setSortingEnabled(True)
            self.table.sortItems(sort_column, sort_order)
            selection = self.table.selectionModel()
            from PySide6.QtCore import QItemSelectionModel
            for row in range(self.table.rowCount()):
                record = self.table.item(row, 0).data(Qt.UserRole)
                if record["id"] in selected:
                    selection.select(self.table.model().index(row, 0),
                                     QItemSelectionModel.Select | QItemSelectionModel.Rows)

    def _mutate(self, callback):
        if self._logged_out:
            return False
        try:
            callback()
        except Exception as exc:
            from core.auth import AuthenticationError
            if isinstance(exc, AuthenticationError):
                self.logout()
                return False
            error(self, "Művelet sikertelen", str(exc))
            return False
        messages = []
        try:
            take_warnings = getattr(self.service, "take_warnings", None)
            if callable(take_warnings):
                messages = list(take_warnings() or [])
        except Exception as exc:
            from core.auth import AuthenticationError
            if isinstance(exc, AuthenticationError):
                self.logout()
                return True
            messages = [
                f"A művelet mentése sikerült, de a figyelmeztetések lekérése nem sikerült: {exc}"
            ]
        self.refresh_data()
        if messages and not self._logged_out and not self._closing:
            warning(self, "Művelet végrehajtva – figyelmeztetés", "\n".join(map(str, messages)))
        return True

    @staticmethod
    def _versions(records):
        return {record["id"]: record["version"] for record in records}

    def _bulk_archive(self, records):
        if confirm(self, "Archiválás", f"Archiválja a kijelölt ({len(records)}) tételt?"):
            self._mutate(lambda: self.service.bulk_update(
                [e["id"] for e in records], {"archivalt": 1}, expected_versions=self._versions(records)))

    def _bulk_unarchive(self, records):
        if confirm(self, "Archiválás", f"Visszavonja a kijelölt ({len(records)}) tétel archiválását?"):
            self._mutate(lambda: self.service.bulk_update(
                [e["id"] for e in records], {"archivalt": 0}, expected_versions=self._versions(records)))

    def _bulk_delete(self, records):
        if confirm(self, "Törlés", f"Törli a kijelölt ({len(records)}) tételt? A csatolmányok megmaradnak."):
            self._mutate(lambda: self.service.delete_permits(
                [e["id"] for e in records], expected_versions=self._versions(records)))

    def del_eng(self, record):
        self._bulk_delete([record])

    def arc_eng(self, record):
        self._mutate(lambda: self.service.bulk_update(
            [record["id"]], {"archivalt": int(not record.get("archivalt"))},
            expected_versions=self._versions([record])))

    def assign_selected(self):
        records = self._get_selected_entities()
        if not records or not self._is_admin():
            return
        users = [user for user in self.data["users"]
                 if user.get("active", 1) and user.get("role") in ("admin", "clerk")]
        labels = ["Nincs felelős"] + [f"{u.get('display_name') or u.get('username')} ({u.get('username')})" for u in users]
        selected, accepted = QInputDialog.getItem(self, "Felelős kijelölése", "Felelős:", labels, 0, False)
        if accepted:
            index = labels.index(selected)
            user_id = users[index - 1]["id"] if index else None
            self._mutate(lambda: self.service.assign(
                [e["id"] for e in records], user_id, expected_versions=self._versions(records)))

    def _on_table_context_menu(self, position):
        records = self._get_selected_entities()
        if not records:
            return
        menu = QMenu(self.table)
        if len(records) == 1:
            menu.addAction("Szerkesztés / bővebb adat", lambda: self.edit_eng(records[0]))
        can_write = all(self._can_write(record) for record in records)
        menu.addAction("Archiválás", lambda: self._bulk_archive(records)).setEnabled(can_write)
        menu.addAction("Archiválás visszavonása", lambda: self._bulk_unarchive(records)).setEnabled(can_write)
        menu.addAction("Törlés", lambda: self._bulk_delete(records)).setEnabled(can_write)
        if self._is_admin():
            menu.addAction("Felelős kijelölése", self.assign_selected)
        menu.addSeparator()
        menu.addAction("Export (PDF) — kiválasztott", lambda: self._export_multiple_records(records))
        if len(records) == 1:
            menu.addAction("Másolás: Határozatszám",
                           lambda: QApplication.clipboard().setText(records[0].get("hatarozatszam") or ""))
        menu.exec(self.table.viewport().mapToGlobal(position))

    def _on_table_double_click(self, row, column):
        item = self.table.item(row, 0)
        if item:
            record = item.data(Qt.UserRole)
            self._open_with_preference(record) if column == 8 else self.edit_eng(record)

    def _delete_selected_via_shortcut(self):
        records = self._get_selected_entities()
        if records:
            self._bulk_delete(records)

    def _on_files_dropped(self, row, paths):
        item = self.table.item(row, 0)
        if item:
            self._add_files_to_entity(item.data(Qt.UserRole), paths)

    def _add_files_to_entity(self, record, paths):
        def save():
            plan = self.service.attachment_plan(record["id"])
            try:
                for path in paths:
                    plan.add(Path(path))
                self.service.save_attachments(record["id"], plan, expected_version=record["version"])
            finally:
                plan.cleanup()
        self._mutate(save)

    def open_telephely_manager(self):
        TelephelyListDialog(self.service, self).exec()
        self.refresh_data()

    def get_hatosag_suggestions(self):
        return sorted({e["kiado_hatosag"] for e in self.data["engedelyek"] if e.get("kiado_hatosag")})

    def add_engedely(self):
        if not self._can_write():
            return
        if not self.data["telephelyek"]:
            warning(self, "Engedély", "Nincs telephely!")
            return
        sites = self.telephely_filter.checked_data()
        categories = self.kategoria_filter.checked_data()
        defaults = {}
        if len(sites) == 1:
            defaults["telephely_id"] = sites[0]
        if len(categories) == 1:
            defaults["kategoria"] = categories[0]
        dialog = self._permit_dialog(defaults=defaults)
        if dialog is None:
            return
        if dialog.exec() == QDialog.Accepted:
            try:
                self._mutate(lambda: self.service.save_permit(
                    self._permit_values(dialog), attachment_plan=dialog.attachment_plan))
            finally:
                dialog.cleanup_attachments()

    def edit_eng(self, record):
        dialog = self._permit_dialog(record)
        if dialog is None:
            return
        if dialog.exec() == QDialog.Accepted:
            try:
                self._mutate(lambda: self.service.save_permit(
                    dict(self._permit_values(dialog), id=record["id"]), expected_version=record["version"],
                    attachment_plan=dialog.attachment_plan))
            finally:
                dialog.cleanup_attachments()

    @staticmethod
    def _permit_values(dialog):
        values = dialog.get_data()
        values.pop("modositok", None)  # The form also supplies canonical modosito_hat.
        values.pop("attachments_temp", None)
        return values

    def _permit_dialog(self, record=None, defaults=None):
        try:
            readonly = not self._can_write(record)
            return EngedelyDialog(
                self, engedely=record, telephelyek=self.data["telephelyek"],
                hatosag_lista=self.get_hatosag_suggestions(), attachments_base=str(self.ATTACHMENTS_DIR),
                service=self.service, readonly=readonly, defaults=defaults)
        except Exception as exc:
            from core.auth import AuthenticationError
            if isinstance(exc, AuthenticationError):
                self.logout()
            else:
                error(self, "Engedély", str(exc))
            return None

    def _open_with_preference(self, record):
        try:
            names = self.service.list_attachments(record["id"])
            if not names:
                info(self, "Fájlok", INFO_NO_ATTACHMENTS)
            else:
                path = self.service.attachment_path(record["id"], names[0])
                if len(names) == 1 and path.suffix.lower() not in ALLOWED_EXTENSIONS:
                    if not confirm(
                        self, "Biztonsági figyelmeztetés",
                        "Ez egy régi, nem támogatott típusú csatolmány, amely programot is "
                        "futtathat. Csak megbízható forrásból származó fájlt nyisson meg.\n"
                        f"Biztosan megnyitja?\n{path.name}",
                    ):
                        return
                    path = self.service.attachment_path(record["id"], names[0])
                _open_path(path if len(names) == 1 else path.parent)
        except Exception as exc:
            from core.auth import AuthenticationError
            if isinstance(exc, AuthenticationError):
                self.logout()
            else:
                error(self, "Fájlok", str(exc))

    def open_backup_dialog(self):
        if self._is_admin():
            BackupRestoreDialog(self.service, self).exec()

    def create_backup_now(self):
        if self._backup_running or not self._is_admin() or self._logged_out:
            return
        from gui.workers import run_task
        self._backup_running = True
        self.btn_backup_now.setEnabled(False)

        def done(path):
            self._backup_running = False
            self.btn_backup_now.setEnabled(True)
            if not self._logged_out and self.isVisible():
                info(self, "Mentés", f"Mentés elkészült: {path}")

        def failed(exc):
            self._backup_running = False
            self.btn_backup_now.setEnabled(True)
            if not self._logged_out and self.isVisible():
                error(self, "Mentés", str(exc))

        run_task(self, self.service.create_backup, done, failed)

    def _export_pdf(self, records, groups=None, mode="nezet", custom_title=None):
        if self._export_running or self._logged_out or self._closing:
            return
        if not records:
            info(self, "Export", "Nincsenek exportálandó engedélyek.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "PDF mentése", f"engedelyek_{date.today():%Y%m%d}.pdf", "PDF (*.pdf)")
        if not path:
            return
        target = Path(path).parent / sanitize_filename(Path(path).name)
        sites = copy.deepcopy(self.data["telephelyek"])
        export_groups = copy.deepcopy(groups or {"Kiválasztott": records})
        logo_path = Path(self.logo_path)
        exporter = export_groups_pdf

        def export():
            kwargs = {
                "mode": mode,
                "logo_path": str(logo_path) if logo_path.exists() else None,
                "telephely_meta": {
                    site["nev"]: {"cim": site.get("cim") or "", "ktj": site.get("ktj") or ""}
                    for site in sites
                },
            }
            if custom_title:
                kwargs["custom_title"] = custom_title
            return exporter(
                str(target), export_groups, {site["id"]: site["nev"] for site in sites}, **kwargs
            )

        self._run_export(export)

    def _run_export(self, function):
        if self._export_running or self._logged_out or self._closing:
            return
        from gui.workers import run_task
        self._export_running = True
        buttons = (self.btn_quick_tp, self.btn_quick_kat, self.btn_excel)
        for button in buttons:
            button.setEnabled(False)
        service = self.service

        def work():
            service.snapshot()
            return function()

        def finish():
            self._export_running = False
            for button in buttons:
                button.setEnabled(True)

        def done(_):
            finish()
            if not self._logged_out and not self._closing:
                info(self, "Export", "Sikeres exportálás.")

        def failed(exc):
            finish()
            if self._logged_out or self._closing:
                return
            from core.auth import AuthenticationError
            if isinstance(exc, AuthenticationError):
                self.logout()
            else:
                error(self, "Export", str(exc))

        run_task(self, work, done, failed)

    def _export_multiple_records(self, records):
        self._export_pdf(records)

    def _export_single_record(self, record):
        self._export_pdf([record])

    def select_items_popup(self, title, items):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout(dialog)
        listing = QListWidget()
        for text in items:
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            listing.addItem(item)
        layout.addWidget(listing)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted:
            return [listing.item(i).text() for i in range(listing.count()) if listing.item(i).checkState() == Qt.Checked]
        return None

    def batch_export(self, mode):
        if self._export_running or self._logged_out or self._closing:
            return
        records = self._get_filtered_items()
        sites = self.data["telephelyek"]
        choices = [s["nev"] for s in sites] if mode == "telephely" else FIX_KATEGORIAK
        selected = self.select_items_popup("Exportálás", choices)
        if not selected:
            return
        names = {s["id"]: s["nev"] for s in sites}
        groups = {}
        for record in records:
            key = names.get(record.get("telephely_id"), "Ismeretlen") if mode == "telephely" else record.get("kategoria")
            if key in selected:
                groups.setdefault(key, []).append(record)
        self._export_pdf([r for group in groups.values() for r in group], groups, mode)

    def export_to_excel(self):
        if self._export_running or self._logged_out or self._closing:
            return
        records = self._get_filtered_items()
        if not records:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Excel mentése", f"export_{date.today():%Y%m%d}.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        export_records = copy.deepcopy(records)
        sites = copy.deepcopy(self.data["telephelyek"])
        options = {"include_archived": self.show_archived.isChecked()}
        exporter = export_excel_fn

        def export():
            return exporter(path, export_records, sites, options=options)

        self._run_export(export)

    def export_current_view(self):
        title, accepted = QInputDialog.getText(self, "Fejléc megadása", "Fejléc:")
        if accepted and title.strip():
            self._export_pdf(self._get_filtered_items(), custom_title=title.strip())

    def open_report_builder(self):
        dialog = ReportBuilderDialog(self, data=self.data, telephelyek=self.data["telephelyek"],
                                      logo_path=str(self.logo_path) if self.logo_path.exists() else None,
                                      initial=self._last_export_options)
        if dialog.exec() == QDialog.Accepted:
            self._last_export_options = dialog.get_options()
            try:
                run_report(self, self._last_export_options, self.data, self.data["telephelyek"],
                           logo_path=str(self.logo_path) if self.logo_path.exists() else None,
                           attachments_dir=str(self.ATTACHMENTS_DIR))
            except Exception as exc:
                error(self, "Kimutatás", str(exc))

    def _expiring_groups(self):
        groups = ([], [], [])
        names = {s["id"]: s["nev"] for s in self.data["telephelyek"]}
        for record in self._get_filtered_items():
            if record.get("archivalt"):
                continue
            validity = record.get("hataly_tipus") or ("indefinite" if record.get("hatarozatlan") else "fixed")
            day = record.get("_lej_dt")
            if validity != "fixed" or not day:
                continue
            days = (day - date.today()).days
            if days <= 180:
                entry = dict(record, telephely_nev=names.get(record.get("telephely_id"), "Ismeretlen"))
                groups[2 if days < 0 else 1 if days <= 90 else 0].append(entry)
        return groups

    def check_expiring_permits(self):
        groups = self._expiring_groups()
        if any(groups):
            ExpiryDialog(*groups, parent=self).show()

    def show_expiring_dialog(self):
        groups = self._expiring_groups()
        if any(groups):
            ExpiryDialog(*groups, parent=self).exec()
        else:
            info(self, "Lejáratok", "Nincsenek lejáró engedélyek a kiválasztott szűrés mellett.")

    def show_reviews_dialog(self):
        reviews = [(record, day, note) for record in self._get_filtered_items() if not record.get("archivalt")
                   for day, note in record.get("_fv_list", [])]
        if reviews:
            ReviewDialog(sorted(reviews, key=lambda item: item[1]), self.data["telephelyek"], self).exec()
        else:
            info(self, "Felülvizsgálatok", "Nincsenek felülvizsgálati tételek a kiválasztott szűrés mellett.")

    def _close_active_popup(self):
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, QDialog) and dialog is not self:
            dialog.reject()
