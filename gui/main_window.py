#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MainWindow - refaktorált, stabilizált és teljesítmény-optimalizált változat.

Kiterjesztés: támogatottá vált a több felülvizsgálati bejegyzés (felulvizsgalatok listája: date+note).
További bővítés: billentyűparancsok és Drag&Drop csatolmány-feltöltés.
"""
from __future__ import annotations

import os
import sys
import shutil
import sqlite3
import logging
import subprocess
import shlex
import json
import csv
from uuid import uuid4
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Set

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QPushButton, QLabel, QLineEdit,
    QCheckBox, QDialog, QDateEdit, QInputDialog, QFileDialog,
    QHeaderView, QGroupBox, QListWidget, QListWidgetItem, QTabWidget,
    QSizePolicy, QDialogButtonBox, QAbstractItemView, QToolButton,
    QMenu, QWidgetAction, QComboBox
)
try:
    from PySide6.QtWidgets import QAction, QShortcut
except Exception:
    from PySide6.QtGui import QAction, QShortcut

from PySide6.QtCore import Qt, QTimer, Signal, QPoint
from PySide6.QtGui import QColor, QFont, QKeySequence

# Try to import openpyxl for real Excel export; if unavailable, we'll fallback to CSV
try:
    from openpyxl import Workbook
    OPENPYXL_AVAILABLE = True
except Exception:
    Workbook = None  # type: ignore
    OPENPYXL_AVAILABLE = False

from config.app_config import get_ordered_categories
from PySide6.QtWidgets import QToolTip
from PySide6.QtGui import QFont

# globális tooltip font + stílus
QToolTip.setFont(QFont("Segoe UI", 10))  # választott betű és méret

app = QApplication.instance()
if app:
    app.setStyleSheet("""
    QToolTip {
        background-color: #e0dada;   /* háttérszín */
        color: #000000;              /* szöveg színe */
        border: 1px solid #000000;
        padding: 2px;                /* belső margó -> "nagyobb méret" érzet */
        font-size: 11pt;             /* opcionális: betűméret CSS-ből is állítható */
    }
    """)


# Robust imports for local dialogs and report builder
TelephelyListDialog = None
EngedelyDialog = None
ReportBuilderDialog = None
run_report = None

# Try package-relative, package-absolute, then script-dir fallback
from gui.telephely_list_dialog import TelephelyListDialog
from gui.engedely_dialog import EngedelyDialog
from gui.report_builder import ReportBuilderDialog, run_report

from exporters.excel_exporter import export_to_excel as export_excel_fn
from exporters.pdf_exporter import export_groups_pdf

from gui.ui_texts import (
    APP_TITLE, BTN_BACKUP, BTN_RESTORE, BTN_OPEN_FOLDER,
    INFO_BACKUP_CREATED, INFO_BACKUP_NONE, INFO_RESTORE_SUCCESS, ERR_RESTORE_FAILED,
    INFO_EXPORT_SUCCESS, ERR_EXPORT_FAILED, INFO_NO_ATTACHMENTS,
    DIALOG_BACKUP_TITLE, DIALOG_ENGEDELY_TITLE
)
from gui.ui_helpers import confirm, info, warning, error

from config import settings
from utils.fs_utils import sanitize_filename, safe_copy_to_dir

logger = logging.getLogger("engedely_nyilvantarto")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(h)
logger.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))

FIX_KATEGORIAK: List[str] = get_ordered_categories()


class AttachDropTable(QTableWidget):
    """
    QTableWidget subclass that accepts file drag & drop onto a row.
    Emits filesDropped(row, [paths]).
    """
    filesDropped = Signal(int, list)

    def __init__(self, rows: int = 0, cols: int = 0, parent=None):
        super().__init__(rows, cols, parent)
        # Accept drops
        self.setAcceptDrops(True)
        # Only allow dropping (not dragging from table)
        try:
            self.setDragDropMode(QAbstractItemView.DropOnly)
        except Exception:
            # older/newer enums differences are tolerated
            pass

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        md = event.mimeData()
        if md.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        md = event.mimeData()
        if not md.hasUrls():
            event.ignore()
            return
        # determine row at drop position
        pos = event.pos()
        row = self.rowAt(pos.y())
        # convert urls to local file paths
        paths = []
        for u in md.urls():
            local = u.toLocalFile()
            if local:
                paths.append(local)
        if row >= 0 and paths:
            self.filesDropped.emit(row, paths)
            event.acceptProposedAction()
        else:
            event.ignore()


class DropdownMultiSelect(QWidget):
    """Dropdown-like multi-select implemented with QListWidget inside a QMenu."""
    selectionChanged = Signal()

    def __init__(self, placeholder: str = "(összes)", parent=None):
        super().__init__(parent)
        self._placeholder = placeholder
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.button = QToolButton(self)
        self.button.setPopupMode(QToolButton.InstantPopup)
        self.button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.button.setText(self._placeholder)

        self.menu = QMenu(self)
        self.listw = QListWidget()
        self.listw.setSelectionMode(QAbstractItemView.ExtendedSelection)
        wa = QWidgetAction(self.menu)
        wa.setDefaultWidget(self.listw)
        self.menu.addAction(wa)

        self.action_ok = self.menu.addAction("Kész")
        self.action_ok.triggered.connect(self._on_done)

        self.button.setMenu(self.menu)
        layout.addWidget(self.button)

        self.listw.itemSelectionChanged.connect(self._on_selection_changed)
        self.menu.aboutToHide.connect(self._on_menu_hide)

    def set_items(self, items: List[tuple]):
        """
        items: list of (text, data, checked?) or (text, data)
        """
        self.listw.clear()
        for it in items:
            if len(it) == 3:
                text, data, checked = it
            else:
                text, data = it
                checked = False
            lw_item = QListWidgetItem(text)
            lw_item.setData(Qt.UserRole, data)
            if checked:
                lw_item.setSelected(True)
            self.listw.addItem(lw_item)
        self._update_display()

    def checked_texts(self) -> List[str]:
        return [it.text() for it in self.listw.selectedItems()]

    def checked_data(self) -> List[Any]:
        return [it.data(Qt.UserRole) for it in self.listw.selectedItems()]

    def set_checked_by_data(self, datas: List[Any]):
        self.listw.blockSignals(True)
        for i in range(self.listw.count()):
            it = self.listw.item(i)
            it.setSelected(it.data(Qt.UserRole) in datas)
        self.listw.blockSignals(False)
        self._update_display()
        self.selectionChanged.emit()

    def _update_display(self):
        texts = self.checked_texts()
        if not texts:
            self.button.setText(self._placeholder)
        elif len(texts) == 1:
            self.button.setText(texts[0])
        else:
            self.button.setText(f"{len(texts)} kiválasztva")

    def _on_done(self):
        # If none selected, select all (convenience)
        if not self.listw.selectedItems():
            for i in range(self.listw.count()):
                self.listw.item(i).setSelected(True)
        self.menu.hide()
        self._update_display()
        self.selectionChanged.emit()

    def _on_selection_changed(self):
        self._update_display()
        self.selectionChanged.emit()

    def _on_menu_hide(self):
        self._update_display()
        self.selectionChanged.emit()


class BackupRestoreDialog(QDialog):
    """Biztonsági mentések dialógusa — megerősítések és üzenetek ui_helpers-szal."""
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle(DIALOG_BACKUP_TITLE)
        self.resize(700, 420)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Rendelkezésre álló mentések:"))
        self.listw = QListWidget()
        v.addWidget(self.listw)

        btns = QHBoxLayout()
        self.btn_backup = QPushButton(BTN_BACKUP)
        self.btn_restore = QPushButton(BTN_RESTORE)
        self.btn_open = QPushButton(BTN_OPEN_FOLDER)
        self.btn_close = QPushButton("Bezár")
        btns.addWidget(self.btn_backup); btns.addWidget(self.btn_restore); btns.addWidget(self.btn_open); btns.addStretch(); btns.addWidget(self.btn_close)
        v.addLayout(btns)

        self.btn_backup.clicked.connect(self._do_backup)
        self.btn_restore.clicked.connect(self._do_restore)
        self.btn_open.clicked.connect(self._do_open)
        self.btn_close.clicked.connect(self.accept)

        self.refresh_list()

    def refresh_list(self):
        self.listw.clear()
        try:
            backs = self.db.list_backups()
        except Exception as e:
            logger.exception("list_backups failed: %s", e)
            backs = []
        for p in backs:
            it = QListWidgetItem(p.name)
            it.setData(Qt.UserRole, p)
            self.listw.addItem(it)

    def _do_backup(self):
        try:
            bak = self.db.make_backup(reason="ui_manual", keep=settings.BACKUP_KEEP)
            if bak:
                info(self, DIALOG_BACKUP_TITLE, INFO_BACKUP_CREATED.format(path=str(bak)))
            else:
                info(self, DIALOG_BACKUP_TITLE, INFO_BACKUP_NONE)
        except Exception as e:
            logger.exception("backup failed: %s", e)
            error(self, DIALOG_BACKUP_TITLE, f"Hiba történt: {e}")
        self.refresh_list()

    def _do_restore(self):
        sel = self.listw.currentItem()
        if not sel:
            info(self, DIALOG_BACKUP_TITLE, "Válasszon egy mentést a listából.")
            return
        p: Path = sel.data(Qt.UserRole)
        if not confirm(self, DIALOG_BACKUP_TITLE, f"Biztosan visszaállítja a mentést?\n{p.name}\n\nAz alkalmazás újraindul a visszaállítás után."):
            return
        try:
            try:
                self.db.make_backup(reason="pre_restore_auto", keep=settings.BACKUP_KEEP)
            except Exception:
                logger.debug("pre-restore auto backup failed, continuing")
            self.db.restore_backup(p)
            info(self, DIALOG_BACKUP_TITLE, INFO_RESTORE_SUCCESS)
            try:
                python = sys.executable
                os.execv(python, [python] + sys.argv)
            except Exception:
                info(self, DIALOG_BACKUP_TITLE, "Nem sikerült automatikusan újraindítani. Indítsa újra kézzel.")
        except Exception as e:
            logger.exception("restore failed: %s", e)
            error(self, DIALOG_BACKUP_TITLE, ERR_RESTORE_FAILED.format(detail=str(e)))

    def _do_open(self):
        sel = self.listw.currentItem()
        if not sel:
            info(self, DIALOG_BACKUP_TITLE, "Válasszon egy mentést a listából.")
            return
        p: Path = sel.data(Qt.UserRole)
        try:
            folder = str(p.parent)
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform.startswith("darwin"):
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            warning(self, DIALOG_BACKUP_TITLE, f"Nem sikerült megnyitni: {e}")


class ExpiryDialog(QDialog):
    def __init__(self, expiring_6m=None, expiring_3m=None, expired=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Lejáró engedélyek")
        self.resize(800, 520)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        def make_list_widget(items, title):
            w = QWidget(); wl = QVBoxLayout(w)
            lbl = QLabel(f"{title} — {len(items)} db")
            lbl.setStyleSheet("font-weight:600; font-size:13px; margin:6px 0;")
            wl.addWidget(lbl)
            lw = QListWidget()
            for item in items:
                lejarat = item.get("lejarat") or "-"
                tp = item.get("telephely_nev", "-")
                kat = item.get("kategoria", "")
                nev = item.get("engedely_nev") or ""
                hatar = item.get("hatarozatszam") or ""
                lw.addItem(f"{lejarat}  |  {tp}  |  {kat} ({nev})  — {hatar}")
            wl.addWidget(lw)
            return w

        tabs.addTab(make_list_widget(expiring_6m or [], "6 hónap (90-180 nap)"), "6 hónap")
        tabs.addTab(make_list_widget(expiring_3m or [], "3 hónap (0-90 nap)"), "3 hónap")
        tabs.addTab(make_list_widget(expired or [], "Már lejárt"), "Lejárt")

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn = btns.button(QDialogButtonBox.Close); close_btn.setText("Bezár")
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)


class ReviewDialog(QDialog):
    """
    Bővített Felülvizsgálatok dialógus.

    - Csoportosítás választható: Telephely szerint / Engedély szerint / Egyenként (lista)
    - Látható a felülvizsgálati dátum, jegyzet, telephely, engedély neve és határozatszám
    - Exportálható .xlsx formátumba (ha openpyxl telepítve), különben CSV fallback
    """
    def __init__(self, review_all=None, telephelyek=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Felülvizsgálatok")
        self.resize(1000, 620)
        self.review_all = review_all or []  # list of (rec, fv_dt, note)
        self.telephelyek = telephelyek or []

        layout = QVBoxLayout(self)

        hdr = QLabel(f"Felülvizsgálatok (összes bejegyzés): {len(self.review_all)}")
        hdr.setStyleSheet("font-weight:600; font-size:13px; margin-bottom:6px;")
        layout.addWidget(hdr)

        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("Csoportosítás:"))
        self.group_combo = QComboBox()
        self.group_combo.addItems(["Telephely szerint", "Engedély szerint", "Lista (összes)"])
        self.group_combo.currentIndexChanged.connect(self._rebuild_view)
        ctrl_row.addWidget(self.group_combo)
        ctrl_row.addStretch()
        # Changed label: export to Excel
        self.btn_export = QPushButton("Exportálás Excel")
        self.btn_export.clicked.connect(self._export_excel)
        ctrl_row.addWidget(self.btn_export)
        layout.addLayout(ctrl_row)

        # main list showing grouped content (simple textual view)
        self.listw = QListWidget()
        self.listw.setSelectionMode(QAbstractItemView.SingleSelection)
        layout.addWidget(self.listw)

        # bottom buttons
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn = btns.button(QDialogButtonBox.Close); close_btn.setText("Bezár")
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        # prepare structured rows for easy grouping and export
        # each entry -> dict with keys: date, note, telephely, kategoria, engedely_nev, hatarozatszam
        self._entries = []
        for rec, fv_dt, note in self.review_all:
            telep = rec.get("telephely_nev") or next((t["nev"] for t in (self.telephelyek or []) if t.get("id") == rec.get("telephely_id")), "Ismeretlen")
            entry = {
                "date": fv_dt,
                "note": note or "",
                "telephely": telep,
                "kategoria": rec.get("kategoria") or "",
                "engedely_nev": rec.get("engedely_nev") or "",
                "hatarozatszam": rec.get("hatarozatszam") or ""
            }
            self._entries.append(entry)

        # initial build
        self._rebuild_view()

    def _rebuild_view(self):
        self.listw.clear()
        mode = self.group_combo.currentText()
        if mode == "Telephely szerint":
            groups: Dict[str, List[Dict[str, Any]]] = {}
            for e in self._entries:
                groups.setdefault(e["telephely"], []).append(e)
            # sort groups by name
            for g in sorted(groups.keys()):
                items = groups[g]
                header = QListWidgetItem(f"{g} — {len(items)} bejegyzés")
                fnt = QFont(); fnt.setBold(True)
                header.setFont(fnt)
                header.setFlags(Qt.ItemIsEnabled)
                self.listw.addItem(header)
                # add entries
                items_sorted = sorted(items, key=lambda x: x["date"] or date.min)
                for it in items_sorted:
                    date_s = it["date"].isoformat() if it["date"] else "-"
                    txt = f"  {date_s}  |  {it['kategoria']}  |  {it['engedely_nev']}  — {it['hatarozatszam']}"
                    if it["note"]:
                        txt += f"  ({it['note']})"
                    li = QListWidgetItem(txt)
                    li.setFlags(Qt.ItemIsEnabled)
                    self.listw.addItem(li)
        elif mode == "Engedély szerint":
            groups: Dict[str, List[Dict[str, Any]]] = {}
            for e in self._entries:
                key = f"{e['kategoria']} — {e['engedely_nev']} ({e['hatarozatszam']})"
                groups.setdefault(key, []).append(e)
            for g in sorted(groups.keys()):
                items = groups[g]
                header = QListWidgetItem(f"{g} — {len(items)} bejegyzés")
                fnt = QFont(); fnt.setBold(True)
                header.setFont(fnt)
                header.setFlags(Qt.ItemIsEnabled)
                self.listw.addItem(header)
                items_sorted = sorted(items, key=lambda x: x["date"] or date.min)
                for it in items_sorted:
                    date_s = it["date"].isoformat() if it["date"] else "-"
                    txt = f"  {date_s}  |  {it['telephely']}  ({it['note']})"
                    li = QListWidgetItem(txt)
                    li.setFlags(Qt.ItemIsEnabled)
                    self.listw.addItem(li)
        else:
            # flat list
            items_sorted = sorted(self._entries, key=lambda x: x["date"] or date.min)
            for it in items_sorted:
                date_s = it["date"].isoformat() if it["date"] else "-"
                txt = f"{date_s}  |  {it['telephely']}  |  {it['kategoria']}  |  {it['engedely_nev']}  — {it['hatarozatszam']}"
                if it["note"]:
                    txt += f"  ({it['note']})"
                li = QListWidgetItem(txt)
                li.setFlags(Qt.ItemIsEnabled)
                self.listw.addItem(li)

    def _export_excel(self):
        """
        Export entries into a real .xlsx file (if openpyxl available).
        Columns requested by user: Dátum, Telephely, engedély, felülvizsgálat tárgya
        We use:
          - Dátum -> entry['date']
          - Telephely -> entry['telephely']
          - Engedély -> entry['engedely_nev']
          - Felülvizsgálat tárgya -> entry['note']
        If openpyxl is not available, fallback to semicolon CSV.
        """
        if not self._entries:
            info(self, "Export", "Nincsenek felülvizsgálati adatok exportáláshoz.")
            return

        default_name = f"felulvizsgalatok_{date.today().strftime('%Y%m%d')}.xlsx"
        path, _ = QFileDialog.getSaveFileName(self, "Exportálás Excel (.xlsx)", default_name, "Excel (*.xlsx);;CSV (*.csv)")
        if not path:
            return
        target = Path(path)
        # If user selected .csv explicitly, honor it (fallback)
        if target.suffix.lower() == ".csv":
            try:
                with target.open("w", encoding="utf-8", newline="") as fh:
                    writer = csv.writer(fh, delimiter=';')
                    writer.writerow(["Dátum", "Telephely", "Engedély", "Felülvizsgálat tárgya"])
                    for it in sorted(self._entries, key=lambda x: x["date"] or date.min):
                        date_s = it["date"].isoformat() if it["date"] else ""
                        writer.writerow([date_s, it["telephely"], it["engedely_nev"], it["note"]])
                info(self, "Export", f"Sikeres exportálás: {str(target)}")
            except Exception as ex:
                logger.exception("reviews CSV export failed: %s", ex)
                error(self, "Export hiba", f"Nem sikerült exportálni: {ex}")
            return

        # For .xlsx export, ensure extension
        if target.suffix.lower() != ".xlsx":
            target = target.with_suffix(".xlsx")

        if OPENPYXL_AVAILABLE:
            try:
                wb = Workbook()
                ws = wb.active
                ws.title = "Felülvizsgálatok"
                # Header
                ws.append(["Dátum", "Telephely", "Engedély", "Felülvizsgálat tárgya"])
                # Rows
                for it in sorted(self._entries, key=lambda x: x["date"] or date.min):
                    date_val = it["date"] if it["date"] else ""
                    # if date_val is a date object, openpyxl will write it correctly
                    ws.append([date_val, it["telephely"], it["engedely_nev"], it["note"]])
                # Optionally set column widths for readability
                try:
                    ws.column_dimensions['A'].width = 15
                    ws.column_dimensions['B'].width = 30
                    ws.column_dimensions['C'].width = 50
                    ws.column_dimensions['D'].width = 50
                except Exception:
                    pass
                wb.save(str(target))
                info(self, "Export", f"Sikeres exportálás: {str(target)}")
            except Exception as ex:
                logger.exception("reviews XLSX export failed: %s", ex)
                error(self, "Export hiba", f"Nem sikerült exportálni Excel-be: {ex}")
        else:
            # Fallback: CSV with a warning
            try:
                with target.with_suffix(".csv").open("w", encoding="utf-8", newline="") as fh:
                    writer = csv.writer(fh, delimiter=';')
                    writer.writerow(["Dátum", "Telephely", "Engedély", "Felülvizsgálat tárgya"])
                    for it in sorted(self._entries, key=lambda x: x["date"] or date.min):
                        date_s = it["date"].isoformat() if it["date"] else ""
                        writer.writerow([date_s, it["telephely"], it["engedely_nev"], it["note"]])
                warning(self, "Export", "openpyxl nincs telepítve — CSV-be exportáltam (Excel kompatibilis).")
            except Exception as ex:
                logger.exception("reviews fallback CSV export failed: %s", ex)
                error(self, "Export hiba", f"Nem sikerült exportálni: {ex}")


class MainWindow(QMainWindow):
    """Főablak — optimalizált implementáció"""

    attachmentsCountReady = Signal(str, int)

    def __init__(self, db, dirs: Optional[Dict[str, Path]] = None, logo_path: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1400, 760)

        self.db = db
        self.dirs = dirs or {}
        self.logo_path = Path(logo_path) if logo_path else Path.cwd() / "logo.png"
        self.ATTACHMENTS_DIR = Path(self.dirs.get("attachments", Path.cwd() / "attachment"))
        self.ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)

        self.last_sync: Optional[str] = None
        self.data: Dict[str, List[Dict[str, Any]]] = {"telephelyek": [], "engedelyek": []}

        # cache: attachments count per eng id
        self._attachments_cache: Dict[str, int] = {}
        # temporary set to avoid recomputing immediately multiple times
        self._attachments_computing: Set[str] = set()

        root = QWidget(); self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)

        # Filters row
        filter_layout = QHBoxLayout()
        self.kategoria_filter = DropdownMultiSelect(placeholder="(összes engedélytípus)")
        self.kategoria_filter.set_items([(kat, kat, False) for kat in FIX_KATEGORIAK])
        self.kategoria_filter.selectionChanged.connect(self.refresh_table)

        self.telephely_filter = DropdownMultiSelect(placeholder="(összes telephely)")
        self.telephely_filter.selectionChanged.connect(self.refresh_table)

        self.show_archived = QCheckBox("Archivált adatok")
        self.show_archived.stateChanged.connect(self.refresh_table)
        self.global_search = QLineEdit()
        self.global_search.setPlaceholderText("Kifejezés keresése")
        # Debounce: 300ms
        self._search_timer = QTimer(self)
        self._search_timer.setInterval(300)
        self._search_timer.setSingleShot(True)
        self._search_timer.timeout.connect(self.refresh_table)
        self.global_search.textChanged.connect(self._on_search_text_changed)

        filter_layout.addWidget(self.kategoria_filter, 2)
        filter_layout.addWidget(self.telephely_filter, 2)
        filter_layout.addWidget(self.show_archived)
        filter_layout.addWidget(self.global_search, 4)
        main_layout.addLayout(filter_layout)

        # Table (use AttachDropTable to support drag&drop)
        self.table = AttachDropTable(0, 9)
        self.table.setHorizontalHeaderLabels([
            "Telephely", "Engedélytípus", "Tárgy", "Határozatszám",
            "Módosító", "Hatálya", "Felülvizsgálat", "Megjegyzés", "Engedély(ek)",
        ])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.cellDoubleClicked.connect(self._on_table_double_click)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Fixed); self.table.setColumnWidth(2, 320)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Interactive)
        header.setSectionResizeMode(5, QHeaderView.Fixed); self.table.setColumnWidth(5, 78)
        header.setSectionResizeMode(6, QHeaderView.Fixed); self.table.setColumnWidth(6, 97)
        header.setSectionResizeMode(7, QHeaderView.Stretch)
        header.setSectionResizeMode(8, QHeaderView.Fixed); self.table.setColumnWidth(8, 105)

        main_layout.addWidget(self.table)

        # Controls row
        self.btn_tel = QPushButton("Telephelyek")
        self.btn_eng = QPushButton("Engedély rögzítés")
        self.btn_quick_tp = QPushButton("🏭 Telephelyenkénti export")
        self.btn_quick_kat = QPushButton("📄 Engedély szerinti export")
        self.btn_excel = QPushButton("📊 Excel Export")
        self.btn_show_expiring = QPushButton("⚠️ Lejáró engedélyek")
        self.btn_reviews = QPushButton("📝 Felülvizsgálatok")

        self.btn_refresh = QPushButton("🔄 Frissítés")
        self.btn_backup_list = QPushButton("🔒 Mentések")
        self.btn_backup_now = QPushButton("⤓ Mentés készítése")
        self.btn_view_export = QPushButton("📊 Kimutatáskészítő")  # opens ReportBuilder
        self.btn_view_export.clicked.connect(self.open_report_builder)

        # Connect slots
        self.btn_tel.clicked.connect(self.open_telephely_manager)
        self.btn_eng.clicked.connect(self.add_engedely)
        self.btn_excel.clicked.connect(self.export_to_excel)
        self.btn_quick_tp.clicked.connect(lambda: self.batch_export("telephely"))
        self.btn_quick_kat.clicked.connect(lambda: self.batch_export("kategoria"))
        self.btn_show_expiring.clicked.connect(self.show_expiring_dialog)
        self.btn_reviews.clicked.connect(self.show_reviews_dialog)
        self.btn_backup_list.clicked.connect(self.open_backup_dialog)
        self.btn_backup_now.clicked.connect(self.create_backup_now)
        self.btn_refresh.clicked.connect(self.manual_refresh)

        add_group = QGroupBox("Adminisztráció")
        ag_layout = QHBoxLayout(); ag_layout.setContentsMargins(8,6,8,6)
        ag_layout.addWidget(self.btn_tel); ag_layout.addWidget(self.btn_eng)
        ag_layout.addWidget(self.btn_show_expiring); ag_layout.addWidget(self.btn_reviews)
        add_group.setLayout(ag_layout)

        export_group = QGroupBox("Exportálás")
        eg_layout = QHBoxLayout(); eg_layout.setContentsMargins(8,6,8,6)
        eg_layout.addWidget(self.btn_quick_tp)
        eg_layout.addWidget(self.btn_quick_kat)
        eg_layout.addWidget(self.btn_excel)
        eg_layout.addWidget(self.btn_view_export)

        export_group.setLayout(eg_layout)

        tools_group = QGroupBox("Rendszer / Mentések")
        tg_layout = QHBoxLayout(); tg_layout.setContentsMargins(8,6,8,6)
        tg_layout.addWidget(self.btn_refresh)
        tg_layout.addWidget(self.btn_backup_now)
        tg_layout.addWidget(self.btn_backup_list)
        tools_group.setLayout(tg_layout)

        btn_row = QHBoxLayout()
        btn_row.addWidget(add_group)
        btn_row.addSpacing(10)
        btn_row.addWidget(export_group)
        btn_row.addSpacing(10)
        btn_row.addStretch()
        btn_row.addWidget(tools_group)
        main_layout.addLayout(btn_row)

        # Timer
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.silent_refresh)
        self.timer.start(300000)  # 5 min

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+B"), self).activated.connect(self.create_backup_now)
        QShortcut(QKeySequence("Ctrl+P"), self).activated.connect(self.open_report_builder)
        QShortcut(QKeySequence("Escape"), self).activated.connect(self._close_active_popup)

        # New shortcuts requested:
        QShortcut(QKeySequence("Ctrl+N"), self).activated.connect(self.add_engedely)
        QShortcut(QKeySequence("Ctrl+R"), self).activated.connect(self.manual_refresh)
        QShortcut(QKeySequence("F5"), self).activated.connect(self.manual_refresh)
        # Delete key should act on selected rows in the table only
        QShortcut(QKeySequence(Qt.Key_Delete), self.table).activated.connect(self._delete_selected_via_shortcut)

        # connect attachment count signal
        self.attachmentsCountReady.connect(self._on_attachments_count_ready)

        # connect drop signal from table to handler
        try:
            self.table.filesDropped.connect(self._on_files_dropped)
        except Exception:
            # If connection fails, ignore — feature optional
            pass

        # Initial load
        try:
            self.refresh_data(incremental=False)
        except Exception:
            logger.exception("Initial refresh_data failed")
        self.refresh_telephely_filter()
        self.refresh_table()
        QTimer.singleShot(0, lambda: self.setWindowState(self.windowState() | Qt.WindowMaximized))

    # ---------------- Report builder entry point ----------------
    def open_report_builder(self) -> None:
        """Open the ReportBuilderDialog and run the report. Shows error if builder is not available."""
        if ReportBuilderDialog is None or run_report is None:
            error(self, "Kimutatás", "Kimutáskészítő nem elérhető (import hiba).")
            return
        try:
            dlg = ReportBuilderDialog(self, data=self.data, telephelyek=self.data.get("telephelyek", []),
                                      logo_path=str(self.logo_path) if self.logo_path.exists() else None,
                                      initial=getattr(self, "_last_export_options", {}))
        except Exception as e:
            logger.exception("Failed to construct ReportBuilderDialog: %s", e)
            error(self, "Kimutatás", f"Kimutatás dialógus létrehozása sikertelen: {e}")
            return
        if dlg.exec() != QDialog.Accepted:
            return
        opts = dlg.get_options()
        self._last_export_options = opts
        try:
            run_report(self, opts, self.data, self.data.get("telephelyek", []),
                       logo_path=str(self.logo_path) if self.logo_path.exists() else None,
                       attachments_dir=str(self.dirs.get("attachments", "")))
        except Exception as ex:
            logger.exception("run_report failed: %s", ex)
            error(self, "Export hiba", ERR_EXPORT_FAILED.format(detail=str(ex)))

    # ---------------- helper: attachment validation & move ----------------
    def _is_extension_allowed(self, path: Path) -> bool:
        ext = path.suffix.lower()
        return ext not in settings.FORBIDDEN_EXTENSIONS

    def _format_mb(self, size_bytes: int) -> str:
        return f"{size_bytes / (1024*1024):.1f}"

    def _move_attachments_from_temp(self, tmp_attach_p: Path, real_attach: Path) -> Dict[str, List[str]]:
        """
        Mozgatja a temp csatolmányokat a valós helyre, miközben ellenőrzi a méretet és kiterjesztést.
        Visszatér egy dict-tel: {"moved": [..], "skipped": [..], "errors":[..]}
        """
        result = {"moved": [], "skipped": [], "errors": []}
        if not tmp_attach_p.exists():
            return result
        real_attach.mkdir(parents=True, exist_ok=True)
        for f in tmp_attach_p.iterdir():
            try:
                if not f.is_file():
                    continue
                try:
                    size = f.stat().st_size
                except Exception:
                    size = 0
                if size > settings.MAX_ATTACHMENT_SIZE:
                    result["skipped"].append(f.name)
                    logger.warning("Attachment skipped (too large): %s (%s MB)", f, self._format_mb(size))
                    continue
                if not self._is_extension_allowed(f):
                    result["skipped"].append(f.name)
                    logger.warning("Attachment skipped (forbidden ext): %s", f)
                    continue
                # safe name and move
                safe_name = sanitize_filename(f.name)
                target = real_attach / safe_name
                # ensure no overwrite unless needed: give unique name if exists
                if target.exists():
                    stem, ext = os.path.splitext(safe_name)
                    idx = 1
                    while True:
                        candidate = real_attach / f"{stem}_{idx}{ext}"
                        if not candidate.exists():
                            target = candidate
                            break
                        idx += 1
                try:
                    shutil.move(str(f), str(target))
                    result["moved"].append(target.name)
                except Exception as e:
                    logger.exception("Failed moving attachment %s -> %s: %s", f, target, e)
                    result["errors"].append(f.name)
            except Exception as e:
                logger.exception("Unexpected error while moving attachment %s: %s", f, e)
                result["errors"].append(f.name if f else "unknown")
        # cleanup tmp dir if empty
        try:
            if tmp_attach_p.exists() and not any(tmp_attach_p.iterdir()):
                tmp_attach_p.rmdir()
        except Exception:
            pass
        return result

    def _add_files_to_entity(self, ent: Dict[str, Any], paths: List[str]) -> None:
        """
        Add local files (paths) into the attachments dir of a given entity.
        Performs checks for size and forbidden extensions, sanitizes names and avoids overwrites.
        Shows info/warnings about skipped/failed files.
        """
        if not ent or not ent.get("id"):
            warning(self, "Csatolmány", "Nem található engedély a célon.")
            return
        eid = ent["id"]
        real_attach = self.ATTACHMENTS_DIR / eid
        real_attach.mkdir(parents=True, exist_ok=True)
        moved = []
        skipped = []
        errors = []
        for p in paths:
            try:
                pth = Path(p)
                if not pth.exists() or not pth.is_file():
                    skipped.append(pth.name if pth.name else str(p))
                    continue
                try:
                    size = pth.stat().st_size
                except Exception:
                    size = 0
                if size > settings.MAX_ATTACHMENT_SIZE:
                    skipped.append(pth.name)
                    logger.warning("Dropped attachment skipped (too large): %s (%s MB)", pth, self._format_mb(size))
                    continue
                if not self._is_extension_allowed(pth):
                    skipped.append(pth.name)
                    logger.warning("Dropped attachment skipped (forbidden ext): %s", pth)
                    continue
                safe_name = sanitize_filename(pth.name)
                target = real_attach / safe_name
                if target.exists():
                    stem, ext = os.path.splitext(safe_name)
                    idx = 1
                    while True:
                        candidate = real_attach / f"{stem}_{idx}{ext}"
                        if not candidate.exists():
                            target = candidate
                            break
                        idx += 1
                try:
                    shutil.copy2(str(pth), str(target))
                    moved.append(target.name)
                except Exception as e:
                    logger.exception("Failed copying dropped file %s -> %s: %s", pth, target, e)
                    errors.append(pth.name)
            except Exception as e:
                logger.exception("Unexpected error while processing dropped file %s: %s", p, e)
                errors.append(Path(p).name if Path(p).name else str(p))
        # update cache and UI
        try:
            cnt = sum(1 for f in real_attach.iterdir() if f.is_file()) if real_attach.exists() else 0
            self._attachments_cache[eid] = cnt
        except Exception:
            logger.exception("Failed updating attachments cache for %s", eid)
        # notify user of results
        msgs = []
        if moved:
            msgs.append(f"{len(moved)} fájl átmásolva: {', '.join(moved)}")
        if skipped:
            msgs.append(f"{len(skipped)} fájl kihagyva (méret / kiterjesztés): {', '.join(skipped)}")
        if errors:
            msgs.append(f"{len(errors)} fájl nem került átmásolásra (hiba): {', '.join(errors)}")
        if msgs:
            info(self, "Csatolmányok", "\n".join(msgs))
        # refresh table so attachment count is visible immediately
        self.refresh_table()

    # ---------------- data / sync / search (robust + precompute search index) ----------------
    def refresh_data(self, incremental: bool = True) -> None:
        try:
            if incremental and self.last_sync:
                telephelyek = []
                changes = []
                try:
                    telephelyek = self.db.get_telephelyek()
                except Exception:
                    logger.exception("get_telephelyek failed")
                try:
                    changes = self.db.get_changes(self.last_sync)
                except Exception:
                    logger.exception("get_changes failed")
                if not self.data.get("engedelyek"):
                    self.data = self.db.get_all_data()
                else:
                    self.data["telephelyek"] = telephelyek
                    cur_map = {e["id"]: e for e in self.data.get("engedelyek", []) if e.get("id")}
                    for ch in changes:
                        if ch.get("id"):
                            cur_map[ch["id"]] = ch
                    self.data["engedelyek"] = list(cur_map.values())
            else:
                self.data = self.db.get_all_data()
        except Exception:
            logger.exception("refresh_data failed")
            self.data = {"telephelyek": [], "engedelyek": []}

        self.last_sync = datetime.now(timezone.utc).isoformat()

        def parse_dt_opt(s: Optional[str]):
            if not s:
                return None
            try:
                return datetime.strptime(s, "%Y-%m-%d").date()
            except Exception:
                try:
                    return datetime.fromisoformat(s).date()
                except Exception:
                    return None

        # Precompute search index and parsed dates to speed up refresh_table and filters
        try:
            tp_map = {t["id"]: t.get("nev", "") for t in self.data.get("telephelyek", [])}
            for e in self.data.get("engedelyek", []):
                # search index
                tp_name = tp_map.get(e.get("telephely_id")) or ""
                fields = " ".join([str(v) for v in (
                    e.get("engedely_nev") or "", e.get("hatarozatszam") or "",
                    e.get("modosito_hat") or "", e.get("kiado_hatosag") or "",
                    e.get("megjegyzes") or "", e.get("kategoria") or "", tp_name
                )])
                e["_search_index"] = fields.casefold()
                # parsed date fields (lejárat)
                e["_lej_dt"] = parse_dt_opt(e.get("lejarat"))

                # New: support multiple felulvizsgalatok entries (JSON string or Python list)
                fv_list_raw = e.get("felulvizsgalatok")
                fv_pairs: List[tuple] = []
                try:
                    if fv_list_raw:
                        if isinstance(fv_list_raw, str):
                            parsed = json.loads(fv_list_raw)
                        else:
                            parsed = fv_list_raw
                        if isinstance(parsed, list):
                            for it in parsed:
                                if isinstance(it, dict):
                                    dt = parse_dt_opt(it.get("date"))
                                    note = it.get("note") or ""
                                    if dt:
                                        fv_pairs.append((dt, note))
                except Exception:
                    logger.debug("Failed to parse felulvizsgalatok for %s", e.get("id"), exc_info=True)

                # Backwards compatibility: if no explicit list, try single 'felulvizsgalat_date' OR computed from nap
                if not fv_pairs:
                    explicit = parse_dt_opt(e.get("felulvizsgalat_date"))
                    if explicit:
                        fv_pairs.append((explicit, ""))

                # computed from nap if applicable (only meaningful if there is a lejarat)
                fv_nap = int(e.get("felulvizsgalat_nap", 0) or 0)
                if fv_nap > 0 and e.get("_lej_dt"):
                    try:
                        computed = e["_lej_dt"] - timedelta(days=fv_nap)
                        # attach as computed with empty note; do not override explicit entries,
                        # but include it as possible next review if no explicit earlier dates
                        fv_pairs.append((computed, f"{fv_nap} nappal a lejárat előtt (számított)"))
                    except Exception:
                        pass

                # unify: sort and keep unique dates (preserve notes for first occurrence)
                unique = {}
                for dt, note in fv_pairs:
                    key = dt.isoformat()
                    if key not in unique:
                        unique[key] = {"date": dt, "note": note}
                # produce list sorted by date
                fv_list_sorted = [ (v["date"], v["note"]) for k, v in sorted(unique.items(), key=lambda kv: kv[0]) ]

                # store machine-friendly structures for UI
                e["_fv_list"] = fv_list_sorted  # list of (date, note)
                # compute "next" fv date to show in table: prefer earliest upcoming (>= today), else earliest historical
                try:
                    today = date.today()
                    upcoming = [d for d, n in fv_list_sorted if d and d >= today]
                    if upcoming:
                        e["_next_fv"] = min(upcoming)
                    else:
                        e["_next_fv"] = fv_list_sorted[0][0] if fv_list_sorted else None
                except Exception:
                    e["_next_fv"] = fv_list_sorted[0][0] if fv_list_sorted else None

            # keep sorted by last_modified/letrejott
            def parse_dt_for_sort(s: Optional[str]):
                if not s:
                    return datetime.min
                try:
                    return datetime.fromisoformat(s)
                except Exception:
                    try:
                        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")
                    except Exception:
                        return datetime.min
            self.data["engedelyek"].sort(key=lambda e: parse_dt_for_sort(e.get("last_modified") or e.get("letrejott")), reverse=True)
        except Exception:
            logger.exception("post-process of refresh_data failed")

    def silent_refresh(self) -> None:
        if hasattr(self, "global_search") and self.global_search.hasFocus():
            return
        try:
            self.refresh_data(incremental=True)
            self.refresh_table()
        except Exception:
            logger.exception("silent_refresh failed")

    # ---------------- UI update / filter ----------------
    def refresh_telephely_filter(self) -> None:
        prev_selected_ids = self.telephely_filter.checked_data()
        items = []
        for t in self.data.get("telephelyek", []):
            items.append((t.get("nev", "Ismeretlen"), t.get("id"), t.get("id") in prev_selected_ids))
        self.telephely_filter.set_items(items)

    def _on_search_text_changed(self, text: str) -> None:
        # restart debounce timer
        self._search_timer.start()

    def _get_filtered_items(self) -> List[Dict[str, Any]]:
        items = list(self.data.get("engedelyek", []))

        search_raw = (self.global_search.text() or "").strip()
        if search_raw:
            try:
                tokens = shlex.split(search_raw)
            except Exception:
                tokens = search_raw.split()
            # fast token matching using precomputed _search_index
            def match_item(e):
                s = e.get("_search_index", "")
                return all(tok.casefold() in s for tok in tokens)
            items = [e for e in items if match_item(e)]

        sel_cats = self.kategoria_filter.checked_texts()
        if sel_cats:
            items = [e for e in items if e.get("kategoria") in sel_cats]
        sel_tp_ids = self.telephely_filter.checked_data()
        if sel_tp_ids:
            items = [e for e in items if e.get("telephely_id") in sel_tp_ids]
        if not self.show_archived.isChecked():
            items = [e for e in items if not e.get("archivalt")]
        return items

    # ---------------- table rendering (optimized) ----------------

    def _attachments_count(self, ent: Dict[str, Any]) -> int:
        """Visszaadja az enthez tartozó csatolmányok fájlszámát (nem rekurzívan)."""
        try:
            eid = ent.get("id")
            if not eid:
                return 0
            # Use cache if available
            if eid in self._attachments_cache:
                return self._attachments_cache[eid]
            adir = self.ATTACHMENTS_DIR / eid
            if not adir.exists() or not adir.is_dir():
                self._attachments_cache[eid] = 0
                return 0
            cnt = sum(1 for f in adir.iterdir() if f.is_file())
            self._attachments_cache[eid] = cnt
            return cnt
        except Exception:
            logger.exception("attachments_count failed for %s", ent.get("id"))
            return 0

    def _ensure_attachments_counts_for_prefix(self, ents: List[Dict[str, Any]], limit: int = 50) -> None:
        """
        Compute attachments count for the first `limit` entries (or fewer).
        This avoids scanning all attachments for large datasets on each refresh.
        """
        try:
            to_check = []
            for e in ents[:limit]:
                eid = e.get("id")
                if not eid:
                    continue
                if eid in self._attachments_cache:
                    continue
                if eid in self._attachments_computing:
                    continue
                to_check.append(eid)
                self._attachments_computing.add(eid)
            # compute synchronously but limited quantity -> acceptable for UI responsiveness
            for eid in to_check:
                try:
                    adir = self.ATTACHMENTS_DIR / eid
                    if not adir.exists() or not adir.is_dir():
                        cnt = 0
                    else:
                        cnt = sum(1 for f in adir.iterdir() if f.is_file())
                    self._attachments_cache[eid] = cnt
                except Exception:
                    self._attachments_cache[eid] = 0
                    logger.exception("counting attachments failed for %s", eid)
                finally:
                    self._attachments_computing.discard(eid)
            # emit updates to refresh visible cells
            for eid in to_check:
                self.attachmentsCountReady.emit(eid, self._attachments_cache.get(eid, 0))
        except Exception:
            logger.exception("ensure_attachments_counts failed")

    def _on_attachments_count_ready(self, eid: str, cnt: int) -> None:
        """
        Update table cells that contain this entity id in column 8.
        """
        try:
            # find rows with that ent and update column 8 text
            for r in range(self.table.rowCount()):
                item0 = self.table.item(r, 0)
                if not item0:
                    continue
                ent = item0.data(Qt.UserRole)
                if ent and ent.get("id") == eid:
                    txt = f"Megnyitás ({cnt})" if cnt > 0 else "Nincs csatolmány"
                    item = self.table.item(r, 8)
                    if item:
                        item.setText(txt)
                        item.setToolTip("Megnyitás: dupla kattintás" if cnt > 0 else "Nincsenek csatolmányok ehhez az engedélyhez.")
        except Exception:
            logger.exception("on_attachments_count_ready failed")

    def refresh_table(self) -> None:
        try:
            self.table.setRowCount(0); self.table.setSortingEnabled(False)
            items = self._get_filtered_items()
            total = len(items)
            self.table.setRowCount(total); today = date.today()

            # Precompute telephely name map for speed
            tp_name_map = {t["id"]: t.get("nev", "Ism.") for t in self.data.get("telephelyek", [])}

            # Ensure attachment counts for first N rows only to keep UI snappy
            try:
                self._ensure_attachments_counts_for_prefix(items, limit=60)
            except Exception:
                logger.exception("attachment ensure precompute failed")

            for r, e in enumerate(items):
                tp_name = tp_name_map.get(e.get("telephely_id"), "Ism.")
                hataly = e.get("hataly_tipus") or ("indefinite" if e.get("hatarozatlan") else "fixed")
                hat_display = e.get("lejarat") or "-" if hataly == "fixed" else ("Határozatlan" if hataly == "indefinite" else "Változtatásig")

                item_tp = QTableWidgetItem(tp_name)
                item_tp.setData(Qt.UserRole, e)
                self.table.setItem(r, 0, item_tp)
                self.table.setItem(r, 1, QTableWidgetItem(e.get("kategoria", "")))
                self.table.setItem(r, 2, QTableWidgetItem(e.get("engedely_nev", "")))
                self.table.setItem(r, 3, QTableWidgetItem(e.get("hatarozatszam", "")))

                mod_text = e.get("modosito_hat", "") or ""
                item_mod = QTableWidgetItem(mod_text)
                if mod_text:
                    parts = [p.strip() for p in mod_text.split(";") if p.strip()]
                    if parts:
                        tooltip_html = ('Módosító határozatok:\n' + "\n".join(parts)) if len(parts) > 1 else ('Módosító határozat:\n' + parts[0])
                        item_mod.setToolTip(tooltip_html)
                self.table.setItem(r, 4, item_mod)
                self.table.setItem(r, 5, QTableWidgetItem(hat_display))

                # Felülvizsgálat
                fv_item = QTableWidgetItem("-")
                # use separate background and foreground variables
                bg_color = QColor(255, 255, 255)
                fg_color = QColor(0, 0, 0)

                # archived rows should be dimmed and must NOT get expiry highlighting
                if e.get("archivalt"):
                    bg_color = QColor("#d1d0d0")
                    fg_color = QColor("#424242")

                lej_dt = e.get("_lej_dt")
                # choose next fv date (precomputed)
                fv_dt = e.get("_next_fv")

                expiry_colored = False
                # apply expiry coloring only for non-archived records
                if not e.get("archivalt") and hataly == "fixed" and lej_dt:
                    try:
                        days_to_expiry = (lej_dt - today).days
                        if lej_dt < today:
                            bg_color = QColor("#ff8383"); fv_item.setForeground(QColor("#d32f2f")); fnt = QFont(); fnt.setBold(True); fv_item.setFont(fnt)
                            expiry_colored = True
                        elif 0 <= days_to_expiry <= 90:
                            bg_color = QColor("#f9c2a0"); fv_item.setForeground(QColor("#F56F02")); fnt = QFont(); fnt.setBold(True); fv_item.setFont(fnt)
                            expiry_colored = True
                        elif 90 < days_to_expiry <= 180:
                            bg_color = QColor("#fff173"); fv_item.setForeground(QColor("#000000"))
                            expiry_colored = True
                    except Exception:
                        expiry_colored = False

                if fv_dt:
                    # build clearer display: show the next date and number of entries; mark if next is "számított"
                    entries = e.get("_fv_list") or []
                    count = len(entries)
                    computed_suffix = ""
                    try:
                        for dtt, note in entries:
                            if dtt == fv_dt and note and "számított" in note:
                                computed_suffix = " *"
                                break
                    except Exception:
                        pass
                    fv_item.setText(f"{fv_dt.isoformat()} ({count}){computed_suffix}")
                    # make tooltip containing all review entries (dates + notes) if available
                    try:
                        if entries:
                            tooltip_lines = []
                            for d, note in entries:
                                if note:
                                    tooltip_lines.append(f"{d.isoformat()}: {note}")
                                else:
                                    tooltip_lines.append(f"{d.isoformat()}")
                            fv_item.setToolTip("\n".join(tooltip_lines) + ("\n\n* a csillag számított (felulvizsgalat_nap alapján) bejegyzést jelez" if computed_suffix else ""))
                    except Exception:
                        pass

                    # keep the existing logic: only compute fv highlighting for non-archived records
                    if not expiry_colored and not e.get("archivalt"):
                        try:
                            days_to_fv = (fv_dt - today).days
                            if fv_dt < today:
                                bg_color = QColor("#ff8383"); fv_item.setForeground(QColor("#d32f2f")); fnt = QFont(); fnt.setBold(True); fv_item.setFont(fnt)
                            elif 0 <= days_to_fv <= 90:
                                bg_color = QColor("#f9c2a0"); fv_item.setForeground(QColor("#f57f17")); fnt = QFont(); fnt.setBold(True); fv_item.setFont(fnt)
                            elif 90 < days_to_fv <= 180:
                                bg_color = QColor("#fff173"); fv_item.setForeground(QColor("#000000"))
                        except Exception:
                            pass

                self.table.setItem(r, 6, fv_item)

                extra_info = ""
                if e.get("kategoria") == "Hulladékgazdálkodási engedély":
                    lines = []
                    if e.get("hulladek_nv_elokez"): lines.append(f"NV-Elő: {e['hulladek_nv_elokez']}")
                    if e.get("hulladek_v_elokez"): lines.append(f"V-Elő: {e['hulladek_v_elokez']}")
                    if e.get("hulladek_nv_gyujt"): lines.append(f"NV-Gyűjt: {e['hulladek_nv_gyujt']}")
                    if e.get("hulladek_v_gyujt"): lines.append(f"V-Gyűjt: {e['hulladek_v_gyujt']}")
                    if lines: extra_info += "[Hulladék: " + ", ".join(lines) + "]"

                item_megj = QTableWidgetItem((e.get("megjegyzes") or "") + " " + extra_info)
                item_megj.setToolTip((e.get("megjegyzes") or "") + "\n" + extra_info)
                self.table.setItem(r, 7, item_megj)

                # last column: lightweight text item with attachments count (no per-row widgets)
                try:
                    files_count = self._attachments_cache.get(e.get("id"), 0)
                except Exception:
                    files_count = 0

                attach_text = f"Megnyitás ({files_count})" if files_count > 0 else "Nincs csatolmány"
                item_btn = QTableWidgetItem(attach_text)
                item_btn.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item_btn.setToolTip("Megnyitás: dupla kattintás" if files_count > 0 else "Nincsenek csatolmányok ehhez a tételhez.")
                self.table.setItem(r, 8, item_btn)

                # set background/foreground for full row cells
                for c in range(9):
                    itm = self.table.item(r, c)
                    if itm:
                        itm.setBackground(bg_color)
                        # only override foreground for archived rows so expiry colors remain on non-archived
                        if e.get("archivalt"):
                            itm.setForeground(fg_color)

            self.table.setSortingEnabled(True)
        except Exception:
            logger.exception("refresh_table failed")

    def _on_table_context_menu(self, pos: QPoint) -> None:
        try:
            selected = self._get_selected_entities()
            if not selected:
                return
            menu = QMenu(self.table)
            if len(selected) == 1:
                ent = selected[0]
                a_edit = QAction("Szerkesztés / bővebb adat", self); a_edit.triggered.connect(lambda _, e=ent: self.edit_eng(e))
                a_archive = QAction("Archiválás" if not ent.get("archivalt") else "Archiválás visszavonása", self); a_archive.triggered.connect(lambda _, e=ent: self.arc_eng(e))
                a_delete = QAction("Törlés", self); a_delete.triggered.connect(lambda _, e=ent: self.del_eng(e))
                menu.addAction(a_edit); menu.addAction(a_archive); menu.addAction(a_delete)
                menu.addSeparator()
                try:
                    files_count = self._attachments_cache.get(ent.get("id"), None)
                    if files_count is None:
                        files_count = self._attachments_count(ent)
                except Exception:
                    files_count = 0
                a_copy = QAction("Másolás: Határozatszám", self); a_copy.triggered.connect(lambda _, e=ent: QApplication.clipboard().setText(str(e.get("hatarozatszam") or "")))
                a_export = QAction("Export (PDF) — csak ez a tétel", self); a_export.triggered.connect(lambda _, e=ent: self._export_single_record(e))
                menu.addAction(a_copy); menu.addAction(a_export)
            else:
                a_bulk_archive = QAction(f"Archiválás kiválasztott ({len(selected)})", self)
                a_bulk_archive.triggered.connect(lambda _, ents=selected: self._bulk_archive(ents))
                a_bulk_unarchive = QAction(f"Archiválás visszavonása kiválasztott ({len(selected)})", self)
                a_bulk_unarchive.triggered.connect(lambda _, ents=selected: self._bulk_unarchive(ents))
                a_bulk_delete = QAction(f"Törlés kiválasztott ({len(selected)})", self)
                a_bulk_delete.triggered.connect(lambda _, ents=selected: self._bulk_delete(ents))
                menu.addAction(a_bulk_archive); menu.addAction(a_bulk_unarchive); menu.addAction(a_bulk_delete)
                menu.addSeparator()
                a_export_sel = QAction("Export (PDF) — kiválasztott", self)
                a_export_sel.triggered.connect(lambda _, ents=selected: self._export_multiple_records(ents))
                menu.addAction(a_export_sel)

            global_pos = self.table.viewport().mapToGlobal(pos)
            menu.exec(global_pos)
        except Exception:
            logger.exception("context menu failed")

    # ---------------- interactions / bulk actions ----------------
    def _on_table_double_click(self, row: int, col: int) -> None:
        try:
            item = self.table.item(row, 0)
            if not item:
                return
            eng = item.data(Qt.UserRole)
            if not eng:
                return
            # double-click on last column opens attachments (if any), otherwise edit
            if col == 8:
                self._open_with_preference(eng)
            else:
                self.edit_eng(eng)
        except Exception:
            logger.exception("double click open edit failed")

    def _get_selected_entities(self) -> List[Dict[str, Any]]:
        sels = self.table.selectionModel().selectedRows()
        ents = []
        for idx in sels:
            item = self.table.item(idx.row(), 0)
            if item:
                ent = item.data(Qt.UserRole)
                if ent:
                    ents.append(ent)
        return ents

    def _bulk_archive(self, ents: List[Dict[str, Any]]) -> None:
        if not confirm(self, "Archiválás", f"Archiválja a kijelölt ({len(ents)}) tételt?"):
            return
        now = datetime.utcnow().isoformat()
        try:
            with self.db.get_connection() as conn:
                for e in ents:
                    conn.execute("UPDATE engedelyek SET archivalt=1, last_modified=? WHERE id=?", (now, e["id"]))
            self._safe_log("engedely_archive_bulk")
            self.refresh_data(incremental=False); self.refresh_table()
        except Exception as ex:
            logger.exception("bulk_archive failed: %s", ex)
            error(self, "Archiválás", f"Archiválás sikertelen: {ex}")

    def _bulk_unarchive(self, ents: List[Dict[str, Any]]) -> None:
        if not confirm(self, "Archiválás visszavonása", f"Visszaállítja archiválást a kijelölt ({len(ents)}) tételeken?"):
            return
        now = datetime.utcnow().isoformat()
        try:
            with self.db.get_connection() as conn:
                for e in ents:
                    conn.execute("UPDATE engedelyek SET archivalt=0, last_modified=? WHERE id=?", (now, e["id"]))
            self._safe_log("bulk_unarchive")
            self.refresh_data(incremental=False); self.refresh_table()
        except Exception as ex:
            logger.exception("bulk_unarchive failed: %s", ex)
            error(self, "Archiválás visszavonása", f"Művelet sikertelen")

    def _bulk_delete(self, ents: List[Dict[str, Any]]) -> None:
        if not confirm(self, "Törlés", f"Végleg törli a kijelölt ({len(ents)}) tételt?"):
            return
        now = datetime.utcnow().isoformat()
        try:
            with self.db.get_connection() as conn:
                for e in ents:
                    conn.execute("UPDATE engedelyek SET deleted=1, last_modified=? WHERE id=?", (now, e["id"]))
                    adir = self.ATTACHMENTS_DIR / e["id"]
                    if adir.exists():
                        try:
                            shutil.rmtree(str(adir))
                        except Exception:
                            pass
            self._safe_log("engedely_delete_bulk")
            self.refresh_data(incremental=False); self.refresh_table()
        except Exception as ex:
            logger.exception("bulk_delete failed: %s", ex)
            error(self, "Törlés", f"Törlés sikertelen: {ex}")

    # ---------------- open helpers ----------------
    def _open_with_preference(self, eng: Dict[str, Any]) -> None:
        """
        If attachments folder contains exactly one file -> open file,
        if zero -> show info, if multiple -> open folder.
        """
        adir = self.ATTACHMENTS_DIR / eng["id"]
        try:
            if not adir.exists() or not adir.is_dir():
                info(self, "Fájlok", INFO_NO_ATTACHMENTS)
                return
            files = [f for f in adir.iterdir() if f.is_file()]
            if len(files) == 0:
                info(self, "Fájlok", INFO_NO_ATTACHMENTS)
                return
            if len(files) == 1:
                fpath = str(files[0])
                if sys.platform.startswith("win"):
                    os.startfile(fpath)
                elif sys.platform.startswith("darwin"):
                    subprocess.Popen(["open", fpath])
                else:
                    subprocess.Popen(["xdg-open", fpath])
            else:
                folder = str(adir)
                if sys.platform.startswith("win"):
                    os.startfile(folder)
                elif sys.platform.startswith("darwin"):
                    subprocess.Popen(["open", folder])
                else:
                    subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            logger.exception("open attachments failed: %s", e)
            warning(self, "Fájlok", f"Nem sikerült megnyitni: {e}")

    def _export_multiple_records(self, ents: List[Dict[str, Any]]) -> None:
        if not callable(export_groups_pdf):
            error(self, "Export", "A PDF exporter nem érhető el.")
            return
        default_name = f"eng_export_{date.today().strftime('%Y%m%d')}.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "PDF mentése (kiválasztott)", default_name, "PDF (*.pdf)")
        if not path:
            return
        # sanitize chosen path: keep dir, sanitize basename
        target_dir = Path(path).parent
        safe_basename = sanitize_filename(Path(path).name)
        target = target_dir / safe_basename

        tp_map = {t["id"]: t.get("nev", "Ismeretlen") for t in self.data.get("telephelyek", [])}
        groups = {"Kiválasztott": ents}
        telephely_meta = {t.get("nev", "Ismeretlen"): {"cim": t.get("cim") or "", "ktj": t.get("ktj") or ""} for t in self.data.get("telephelyek", [])}
        logo_path = str(self.logo_path) if self.logo_path.exists() else None
        try:
            export_groups_pdf(str(target), groups, tp_map, mode="nezet", logo_path=logo_path, telephely_meta=telephely_meta)
            info(self, "Exportálás", INFO_EXPORT_SUCCESS)
        except Exception as ex:
            logger.exception("export multiple failed: %s", ex)
            error(self, "PDF hiba", ERR_EXPORT_FAILED.format(detail=str(ex)))

    # ---------------- CRUD / actions (single) ----------------
    def open_backup_dialog(self) -> None:
        try:
            dlg = BackupRestoreDialog(self.db, self)
            dlg.exec()
        except Exception as e:
            logger.exception("open_backup_dialog failed: %s", e)
            error(self, "Mentések", f"Mentések megnyitása sikertelen: {e}")

    def create_backup_now(self) -> None:
        try:
            bak = self.db.make_backup(reason="ui_manual", keep=settings.BACKUP_KEEP)
            if bak:
                info(self, "Mentés", INFO_BACKUP_CREATED.format(path=str(bak)))
            else:
                info(self, "Mentés", INFO_BACKUP_NONE)
            self._safe_log("backup_manual")
        except Exception as e:
            logger.exception("backup_now failed: %s", e)
            error(self, "Mentés", f"Mentés sikertelen: {e}")

    def open_telephely_manager(self) -> None:
        if TelephelyListDialog is None:
            error(self, "Telephelyek", "Telephely kezelő nem elérhető (import hiba).")
            return
        try:
            dlg = TelephelyListDialog(self.db, self)
            dlg.exec()
        except Exception as e:
            logger.exception("open_telephely_manager failed: %s", e)
            error(self, "Telephelyek", f"Telephely kezelő megnyitása sikertelen: {e}")
            return
        self.refresh_data(incremental=False); self.refresh_telephely_filter(); self.refresh_table()

    def add_engedely(self) -> None:
        if EngedelyDialog is None:
            error(self, "Engedély", "Engedély dialog nem elérhető (import hiba).")
            return
        if not self.data.get("telephelyek"):
            warning(self, DIALOG_ENGEDELY_TITLE, "Nincs telephely!")
            return
        dlg = EngedelyDialog(self, engedely=None, telephelyek=self.data["telephelyek"], hatosag_lista=self.get_hatosag_suggestions(), attachments_base=str(self.ATTACHMENTS_DIR))
        res = dlg.exec()
        if res == QDialog.Accepted:
            d = dlg.get_data()
            if not d.get("telephely_id") or not d.get("hatarozatszam"):
                warning(self, DIALOG_ENGEDELY_TITLE, "Telephely és határozat száma megadása kötelező.")
                return
            new_id = str(uuid4()); now = datetime.utcnow().isoformat()
            try:
                # prepare felulvizsgalatok JSON (if present)
                felu_json = None
                if d.get("felulvizsgalatok"):
                    try:
                        felu_json = json.dumps(d.get("felulvizsgalatok"), ensure_ascii=False)
                    except Exception:
                        felu_json = None

                with self.db.get_connection() as conn:
                    conn.execute("""
                        INSERT INTO engedelyek (
                            id, telephely_id, engedely_nev, kategoria, kiado_hatosag, hatarozatszam,
                            modosito_hat, letrejott, hatarozatlan, lejarat, hataly_tipus, felulvizsgalat_nap, felulvizsgalat_date, felulvizsgalatok, megjegyzes, archivalt,
                            hulladek_nv_elokez, hulladek_v_elokez, hulladek_nv_gyujt, hulladek_v_gyujt, last_modified, deleted
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (new_id, d["telephely_id"], d["engedely_nev"], d["kategoria"],
                          d["kiado_hatosag"], d["hatarozatszam"], d.get("modosito_hat"),
                          d.get("letrejott"), d.get("hatarozatlan", 0), d.get("lejarat"),
                          d.get("hataly_tipus", "fixed"), d.get("felulvizsgalat_nap", 0), d.get("felulvizsgalat_date"),
                          felu_json, d.get("megjegyzes"), 0,
                          d.get("hulladek_nv_elokez"), d.get("hulladek_v_elokez"), d.get("hulladek_nv_gyujt"), d.get("hulladek_v_gyujt"),
                          now, 0))
                tmp_attach = d.get("attachments_temp")
                if tmp_attach:
                    tmp_attach_p = Path(tmp_attach)
                    if tmp_attach_p.exists():
                        real_attach = self.ATTACHMENTS_DIR / new_id
                        moved = self._move_attachments_from_temp(tmp_attach_p, real_attach)
                        skipped = moved.get("skipped") or []
                        if skipped:
                            warning(self, "Csatolmányok", f"{len(skipped)} fájl nem került átmásolásra a korlátozások miatt: {', '.join(skipped)}")
                self._safe_log("engedely_insert")
                try:
                    rec = {
                        "id": new_id, "telephely_id": d["telephely_id"], "engedely_nev": d["engedely_nev"],
                        "kategoria": d["kategoria"], "kiado_hatosag": d.get("kiado_hatosag"), "hatarozatszam": d["hatarozatszam"],
                        "modosito_hat": d.get("modosito_hat"), "letrejott": d.get("letrejott"), "hatarozatlan": d.get("hatarozatlan", 0),
                        "lejarat": d.get("lejarat"), "hataly_tipus": d.get("hataly_tipus", "fixed"),
                        "felulvizsgalat_nap": d.get("felulvizsgalat_nap", 0), "felulvizsgalat_date": d.get("felulvizsgalat_date"),
                        "megjegyzes": d.get("megjegyzes"), "archivalt": 0,
                        "hulladek_nv_elokez": d.get("hulladek_nv_elokez"), "hulladek_v_elokez": d.get("hulladek_v_elokez"),
                        "hulladek_nv_gyujt": d.get("hulladek_nv_gyujt"), "hulladek_v_gyujt": d.get("hulladek_v_gyujt"),
                        "last_modified": now, "deleted": 0
                    }
                    # include felulvizsgalatok in-memory if provided by dialog (DB column may not exist)
                    if d.get("felulvizsgalatok"):
                        rec["felulvizsgalatok"] = d.get("felulvizsgalatok")
                    self.data["engedelyek"].insert(0, rec)
                except Exception:
                    self.refresh_data(incremental=False)
                self.refresh_table()
            except sqlite3.OperationalError as e:
                logger.exception("add_engedely OperationalError: %s", e)
                error(self, "Adatbázis", f"Adatbázis hiba: {e}")

    def edit_eng(self, eng: Dict[str, Any]) -> None:
        if EngedelyDialog is None:
            error(self, "Engedély", "Engedély dialog nem elérhető (import hiba).")
            return
        dlg = EngedelyDialog(self, eng, self.data.get("telephelyek", []), self.get_hatosag_suggestions(), attachments_base=str(self.ATTACHMENTS_DIR))
        res = dlg.exec()
        if res == QDialog.Accepted:
            d = dlg.get_data()
            if not d.get("telephely_id") or not d.get("hatarozatszam"):
                warning(self, "Hiba", "Telephely és határozat száma megadása kötelező.")
                return
            now = datetime.utcnow().isoformat()
            try:
                # prepare felulvizsgalatok JSON (if present)
                felu_json = None
                if d.get("felulvizsgalatok"):
                    try:
                        felu_json = json.dumps(d.get("felulvizsgalatok"), ensure_ascii=False)
                    except Exception:
                        felu_json = None

                with self.db.get_connection() as conn:
                    conn.execute("""
                        UPDATE engedelyek SET
                        telephely_id=?, engedely_nev=?, kategoria=?, kiado_hatosag=?,
                        hatarozatszam=?, modosito_hat=?, letrejott=?, hatarozatlan=?,
                        lejarat=?, hataly_tipus=?, felulvizsgalat_nap=?, felulvizsgalat_date=?, felulvizsgalatok=?, megjegyzes=?,
                        hulladek_nv_elokez=?, hulladek_v_elokez=?, hulladek_nv_gyujt=?, hulladek_v_gyujt=?, last_modified=?
                        WHERE id=?
                    """, (d["telephely_id"], d["engedely_nev"], d["kategoria"], d.get("kiado_hatosag"),
                          d["hatarozatszam"], d.get("modosito_hat"), d.get("letrejott"), d.get("hatarozatlan", 0),
                          d.get("lejarat"), d.get("hataly_tipus", "fixed"), d.get("felulvizsgalat_nap", 0), d.get("felulvizsgalat_date"),
                          felu_json, d.get("megjegyzes"),
                          d.get("hulladek_nv_elokez"), d.get("hulladek_v_elokez"), d.get("hulladek_nv_gyujt"), d.get("hulladek_v_gyujt"),
                          now, eng["id"]))
                tmp_attach = d.get("attachments_temp")
                if tmp_attach:
                    tmp_attach_p = Path(tmp_attach)
                    if tmp_attach_p.exists():
                        real_attach = self.ATTACHMENTS_DIR / eng["id"]
                        moved = self._move_attachments_from_temp(tmp_attach_p, real_attach)
                        skipped = moved.get("skipped") or []
                        if skipped:
                            warning(self, "Csatolmányok", f"{len(skipped)} fájl nem került átmásolásra a korlátozások miatt: {', '.join(skipped)}")
                        # invalidate cache for this id
                        self._attachments_cache.pop(eng["id"], None)
                self._safe_log("engedely_update")
                try:
                    idx = next((i for i, x in enumerate(self.data.get("engedelyek", [])) if x.get("id") == eng.get("id")), None)
                    if idx is not None:
                        self.data["engedelyek"][idx].update({
                            "telephely_id": d["telephely_id"], "engedely_nev": d["engedely_nev"], "kategoria": d["kategoria"],
                            "kiado_hatosag": d.get("kiado_hatosag"), "hatarozatszam": d["hatarozatszam"], "modosito_hat": d.get("modosito_hat"),
                            "letrejott": d.get("letrejott"), "hatarozatlan": d.get("hatarozatlan", 0), "lejarat": d.get("lejarat"),
                            "hataly_tipus": d.get("hataly_tipus", "fixed"), "felulvizsgalat_nap": d.get("felulvizsgalat_nap", 0),
                            "felulvizsgalat_date": d.get("felulvizsgalat_date"), "megjegyzes": d.get("megjegyzes"), "hulladek_nv_elokez": d.get("hulladek_nv_elokez"),
                            "hulladek_v_elokez": d.get("hulladek_v_elokez"), "hulladek_nv_gyujt": d.get("hulladek_nv_gyujt"),
                            "hulladek_v_gyujt": d.get("hulladek_v_gyujt"), "last_modified": now
                        })
                        # include felulvizsgalatok in-memory if provided by dialog
                        if d.get("felulvizsgalatok"):
                            self.data["engedelyek"][idx]["felulvizsgalatok"] = d.get("felulvizsgalatok")
                        # update precomputed fields for this record (recompute _fv_list/_next_fv for immediate UI)
                        updated = self.data["engedelyek"][idx]
                        tp_name = next((t["nev"] for t in self.data.get("telephelyek", []) if t["id"] == updated.get("telephely_id")), "")
                        fields = " ".join([str(v) for v in (
                            updated.get("engedely_nev") or "", updated.get("hatarozatszam") or "",
                            updated.get("modosito_hat") or "", updated.get("kiado_hatosag") or "",
                            updated.get("megjegyzes") or "", updated.get("kategoria") or "", tp_name
                        )])
                        updated["_search_index"] = fields.casefold()
                        # recompute parsed dates similar to refresh_data logic
                        try:
                            if updated.get("lejarat"):
                                updated["_lej_dt"] = datetime.strptime(updated["lejarat"], "%Y-%m-%d").date()
                            else:
                                updated["_lej_dt"] = None
                        except Exception:
                            updated["_lej_dt"] = None

                        # parse felulvizsgalatok
                        fv_list_raw = updated.get("felulvizsgalatok")
                        fv_pairs: List[tuple] = []
                        try:
                            if fv_list_raw:
                                if isinstance(fv_list_raw, str):
                                    parsed = json.loads(fv_list_raw)
                                else:
                                    parsed = fv_list_raw
                                if isinstance(parsed, list):
                                    for it in parsed:
                                        if isinstance(it, dict):
                                            dt = None
                                            try:
                                                dt = datetime.strptime(it.get("date"), "%Y-%m-%d").date()
                                            except Exception:
                                                try:
                                                    dt = datetime.fromisoformat(it.get("date")).date()
                                                except Exception:
                                                    dt = None
                                            note = it.get("note") or ""
                                            if dt:
                                                fv_pairs.append((dt, note))
                        except Exception:
                            pass
                        if not fv_pairs:
                            try:
                                if updated.get("felulvizsgalat_date"):
                                    ddt = datetime.strptime(updated["felulvizsgalat_date"], "%Y-%m-%d").date()
                                    fv_pairs.append((ddt, ""))
                            except Exception:
                                pass
                        try:
                            fv_nap = int(updated.get("felulvizsgalat_nap", 0) or 0)
                        except Exception:
                            fv_nap = 0
                        if fv_nap > 0 and updated.get("_lej_dt"):
                            try:
                                computed = updated["_lej_dt"] - timedelta(days=fv_nap)
                                fv_pairs.append((computed, f"{fv_nap} nappal a lejárat előtt (számított)"))
                            except Exception:
                                pass
                        unique = {}
                        for dt, note in fv_pairs:
                            key = dt.isoformat()
                            if key not in unique:
                                unique[key] = {"date": dt, "note": note}
                        fv_list_sorted = [ (v["date"], v["note"]) for k, v in sorted(unique.items(), key=lambda kv: kv[0]) ]
                        updated["_fv_list"] = fv_list_sorted
                        try:
                            today = date.today()
                            upcoming = [d for d, n in fv_list_sorted if d and d >= today]
                            if upcoming:
                                updated["_next_fv"] = min(upcoming)
                            else:
                                updated["_next_fv"] = fv_list_sorted[0][0] if fv_list_sorted else None
                        except Exception:
                            updated["_next_fv"] = fv_list_sorted[0][0] if fv_list_sorted else None
                    else:
                        self.refresh_data(incremental=False)
                except Exception:
                    self.refresh_data(incremental=False)
                self.refresh_table()
            except sqlite3.OperationalError:
                error(self, "Hiba", "Az adatbázis foglalt. Módosítás sikertelen.")

    def del_eng(self, eng: Dict[str, Any]) -> None:
        if confirm(self, "Törlés", "Véglegesen törli ezt az engedélyt?"):
            try:
                now = datetime.utcnow().isoformat()
                with self.db.get_connection() as conn:
                    conn.execute("UPDATE engedelyek SET deleted=1, last_modified=? WHERE id=?", (now, eng["id"]))
                adir = self.ATTACHMENTS_DIR / eng["id"]
                if adir.exists():
                    try:
                        shutil.rmtree(str(adir))
                    except Exception:
                        pass
                # invalidate cache
                self._attachments_cache.pop(eng["id"], None)
                self._safe_log("engedely_delete")
                try:
                    self.data["engedelyek"] = [x for x in self.data.get("engedelyek", []) if x.get("id") != eng.get("id")]
                except Exception:
                    pass
                self.refresh_table()
            except sqlite3.OperationalError:
                error(self, "Hiba", "Nem sikerült törölni a zárolás miatt.")

    def arc_eng(self, eng: Dict[str, Any]) -> None:
        new_state = 0 if eng.get("archivalt") else 1
        now = datetime.utcnow().isoformat()
        try:
            with self.db.get_connection() as conn:
                conn.execute("UPDATE engedelyek SET archivalt=?, last_modified=? WHERE id=?", (new_state, now, eng["id"]))
            self._safe_log("engedely_archive_toggle")
            try:
                for x in self.data.get("engedelyek", []):
                    if x.get("id") == eng.get("id"):
                        x["archivalt"] = new_state; x["last_modified"] = now; break
            except Exception:
                pass
            self.refresh_table()
        except sqlite3.OperationalError:
            error(self, "Hiba", "Nem sikerült archiválni.")

    # ---------------- export / helper public methods ----------------
    def select_items_popup(self, title: str, items: List[str]) -> Optional[List[str]]:
        dlg = QDialog(self); dlg.setWindowTitle(title); dlg.resize(350,400)
        layout = QVBoxLayout(dlg); layout.addWidget(QLabel("Válassza ki az exportálandó elemeket:"))
        lw = QListWidget()
        for it in items:
            item = QListWidgetItem(it)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Checked)
            lw.addItem(item)
        layout.addWidget(lw)
        btns = QHBoxLayout(); ok_btn = QPushButton("Export"); cancel_btn = QPushButton("Mégse")
        btns.addStretch(); btns.addWidget(ok_btn); btns.addWidget(cancel_btn); layout.addLayout(btns)
        ok_btn.clicked.connect(dlg.accept); cancel_btn.clicked.connect(dlg.reject)
        if dlg.exec() != QDialog.Accepted: return None
        selected = [lw.item(i).text() for i in range(lw.count()) if lw.item(i).checkState() == Qt.Checked]
        return selected

    def batch_export(self, mode: str) -> None:
        if not callable(export_groups_pdf):
            error(self, "Export", "A PDF exporter nem érhető el.")
            return
        items = [e for e in self.data.get("engedelyek", []) if not e.get("archivalt")]
        if not items:
            info(self, "Export", "Nincsenek exportálandó engedélyek.")
            return

        if mode == "telephely":
            all_tp = [t.get("nev", "Ismeretlen") for t in self.data.get("telephelyek", [])]
            selected = self.select_items_popup("Telephelyek kiválasztása", all_tp)
        else:
            selected = self.select_items_popup("Engedélytípusok kiválasztása", FIX_KATEGORIAK)
        if not selected:
            return

        date_s = date.today().strftime('%Y%m%d')
        default_name = f"Engedelynyilvantartas_{date_s}.pdf" if mode != "telephely" else f"Engedelynyilvantartas_telephelyi_{date_s}.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "PDF mentése", default_name, "PDF (*.pdf)")
        if not path:
            return

        # sanitize
        target_dir = Path(path).parent
        safe_basename = sanitize_filename(Path(path).name)
        target = target_dir / safe_basename

        tp_map = {t["id"]: t.get("nev", "Ismeretlen") for t in self.data.get("telephelyek", [])}
        groups = {}
        if mode == "telephely":
            for tp in self.data.get("telephelyek", []):
                name = tp.get("nev", "Ismeretlen")
                if name not in selected: continue
                rel = [e for e in items if e.get("telephely_id") == tp.get("id")]
                if rel: groups[name] = rel
        else:
            for kat in FIX_KATEGORIAK:
                if kat not in selected: continue
                rel = [e for e in items if e.get("kategoria") == kat]
                if rel: groups[kat] = rel
        if not groups:
            info(self, "Export", "A kiválasztott csoportokhoz nincs exportálandó adat.")
            return
        telephely_meta = {t.get("nev", "Ismeretlen"): {"cim": t.get("cim") or "", "ktj": t.get("ktj") or ""} for t in self.data.get("telephelyek", [])}
        logo_path = str(self.logo_path) if self.logo_path.exists() else None
        try:
            export_groups_pdf(str(target), groups, tp_map, mode=mode, logo_path=logo_path, telephely_meta=telephely_meta)
            info(self, "Exportálás", INFO_EXPORT_SUCCESS)
        except Exception as ex:
            logger.exception("batch_export failed: %s", ex)
            error(self, "PDF hiba", ERR_EXPORT_FAILED.format(detail=str(ex)))

    def export_to_excel(self) -> None:
        items = self.data.get("engedelyek", [])
        if not items:
            warning(self, "Excel export", "Nincs exportálható adat!")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Excel mentése", f"export_{date.today().strftime('%Y%m%d')}.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        # sanitize
        target_dir = Path(path).parent
        safe_basename = sanitize_filename(Path(path).name)
        target = target_dir / safe_basename
        try:
            # call exporter - it may accept options in newer versions
            try:
                export_excel_fn(str(target), items, self.data.get("telephelyek", []), options={})
            except TypeError:
                export_excel_fn(str(target), items, self.data.get("telephelyek", []))
            info(self, "Siker", "Az adatbázis exportálása .xlsx formátumba megtörtént!")
        except Exception as e:
            logger.exception("export_to_excel failed: %s", e)
            error(self, "Hiba", f"Nem sikerült a mentés: {str(e)}")

    # The specialized export_current_view fallback preserved only for compatibility.
    def export_current_view(self) -> None:
        items = self._get_filtered_items()
        if not items:
            info(self, "Export", "A jelenlegi nézetben nincs exportálandó adat.")
            return
        header_text, ok = QInputDialog.getText(self, "Fejléc megadása", "Adja meg a fejléc szövegét:")
        if not ok or not header_text.strip():
            info(self, "Export", "A fejléc megadása kötelező a nézet exportálásához.")
            return
        header_text = header_text.strip()
        path, _ = QFileDialog.getSaveFileName(self, "PDF mentése (nézet)", "", "PDF (*.pdf)")
        if not path:
            return
        # sanitize
        target_dir = Path(path).parent
        safe_basename = sanitize_filename(Path(path).name)
        target = target_dir / safe_basename
        tp_map = {t["id"]: t.get("nev", "Ismeretlen") for t in self.data.get("telephelyek", [])}
        groups = {"Nézet": items}
        telephely_meta = {t.get("nev", "Ismeretlen"): {"cim": t.get("cim") or "", "ktj": t.get("ktj") or ""} for t in self.data.get("telephelyek", [])}
        logo_path = str(self.logo_path) if self.logo_path.exists() else None
        try:
            export_groups_pdf(str(target), groups, tp_map, mode="nezet", logo_path=logo_path, telephely_meta=telephely_meta, custom_title=header_text)
            info(self, "Exportálás", INFO_EXPORT_SUCCESS)
        except Exception as ex:
            logger.exception("export_current_view failed: %s", ex)
            error(self, "PDF hiba", ERR_EXPORT_FAILED.format(detail=str(ex)))

    def _export_single_record(self, ent: Dict[str, Any]) -> None:
        if not callable(export_groups_pdf):
            error(self, "Export", "A PDF exporter nem érhető el.")
            return
        default_name = f"engedely_{ent.get('hatarozatszam', '')}_{date.today().strftime('%Y%m%d')}.pdf"
        path, _ = QFileDialog.getSaveFileName(self, "PDF mentése (tétel)", default_name, "PDF (*.pdf)")
        if not path:
            return
        # sanitize
        target_dir = Path(path).parent
        safe_basename = sanitize_filename(Path(path).name)
        target = target_dir / safe_basename
        tp_map = {t["id"]: t.get("nev", "Ismeretlen") for t in self.data.get("telephelyek", [])}
        groups = {"Engedély": [ent]}
        telephely_meta = {t.get("nev", "Ismeretlen"): {"cim": t.get("cim") or "", "ktj": t.get("ktj") or ""} for t in self.data.get("telephelyek", [])}
        logo_path = str(self.logo_path) if self.logo_path.exists() else None
        try:
            export_groups_pdf(str(target), groups, tp_map, mode="single", logo_path=logo_path, telephely_meta=telephely_meta)
            info(self, "Exportálás", INFO_EXPORT_SUCCESS)
        except Exception as ex:
            logger.exception("export single failed: %s", ex)
            error(self, "PDF hiba", ERR_EXPORT_FAILED.format(detail=str(ex)))

    def _safe_log(self, note: Optional[str]) -> None:
        try:
            fn = getattr(self.db, "log_event", None)
            if callable(fn):
                fn(note)
        except Exception:
            logger.debug("log_event failed", exc_info=True)

    def get_hatosag_suggestions(self) -> List[str]:
        return sorted(list({e.get("kiado_hatosag", "") for e in self.data.get("engedelyek", []) if e.get("kiado_hatosag")}))

    def manual_refresh(self) -> None:
        try:
            self.refresh_data(incremental=False)
            self.refresh_telephely_filter()
            self.refresh_table()
            self._safe_log("manual_refresh")
        except Exception:
            logger.exception("manual_refresh failed")

    # ---------------- expiry / reviews ----------------
    def check_expiring_permits(self) -> None:
        today = date.today()
        expiring_6m = []; expiring_3m = []; expired = []
        for e in self.data.get("engedelyek", []):
            if e.get("archivalt"):
                continue
            hataly = e.get("hataly_tipus") or ("indefinite" if e.get("hatarozatlan") else "fixed")
            if hataly != "fixed":
                continue
            if not e.get("lejarat"):
                continue
            try:
                lej_dt = e.get("_lej_dt")
                if not lej_dt:
                    continue
                days = (lej_dt - today).days
                tp = next((t["nev"] for t in self.data.get("telephelyek", []) if t["id"] == e["telephely_id"]), "Ismeretlen")
                rec = e.copy(); rec["telephely_nev"] = tp
                if days < 0:
                    expired.append(rec)
                elif 0 <= days <= 90:
                    expiring_3m.append(rec)
                elif 90 < days <= 180:
                    expiring_6m.append(rec)
            except Exception:
                continue

        if expiring_6m or expiring_3m or expired:
            ExpiryDialog(expiring_6m=expiring_6m, expiring_3m=expiring_3m, expired=expired, parent=self).show()

    def show_expiring_dialog(self) -> None:
        today = date.today()
        expiring_6m = []; expiring_3m = []; expired = []
        for e in self.data.get("engedelyek", []):
            if e.get("archivalt"):
                continue
            hataly = e.get("hataly_tipus") or ("indefinite" if e.get("hatarozatlan") else "fixed")
            if hataly != "fixed":
                continue
            if not e.get("lejarat"):
                continue
            try:
                lej_dt = e.get("_lej_dt")
                if not lej_dt:
                    continue
                days = (lej_dt - today).days
                tp = next((t["nev"] for t in self.data.get("telephelyek", []) if t["id"] == e["telephely_id"]), "Ismeretlen")
                rec = e.copy(); rec["telephely_nev"] = tp
                if days < 0:
                    expired.append(rec)
                elif 0 <= days <= 90:
                    expiring_3m.append(rec)
                elif 90 < days <= 180:
                    expiring_6m.append(rec)
            except Exception:
                continue

        if expiring_6m or expiring_3m or expired:
            ExpiryDialog(expiring_6m=expiring_6m, expiring_3m=expiring_3m, expired=expired, parent=self).exec()
        else:
            info(self, "Lejáratok", "Nincsenek lejáró engedélyek a kiválasztott szűrés mellett.")

    def show_reviews_dialog(self) -> None:
        review_all = []
        for e in self.data.get("engedelyek", []):
            if e.get("archivalt"):
                continue

            # Use explicit list of review entries if present
            fv_list = e.get("_fv_list") or []
            for fv_dt, note in fv_list:
                tp = next((t["nev"] for t in self.data.get("telephelyek", []) if t["id"] == e["telephely_id"]), "Ismeretlen")
                rec = e.copy(); rec["telephely_nev"] = tp
                review_all.append((rec, fv_dt, note))

            # If no explicit entries but a computed fv (from felulvizsgalat_nap), include it (compat)
            if not fv_list:
                fv_nap = int(e.get("felulvizsgalat_nap", 0) or 0)
                lejarat_date = e.get("_lej_dt")
                if fv_nap > 0 and lejarat_date:
                    try:
                        computed = lejarat_date - timedelta(days=fv_nap)
                        tp = next((t["nev"] for t in self.data.get("telephelyek", []) if t["id"] == e["telephely_id"]), "Ismeretlen")
                        rec = e.copy(); rec["telephely_nev"] = tp
                        review_all.append((rec, computed, f"{fv_nap} nappal a lejárat előtt (számított)"))
                    except Exception:
                        pass

        review_sorted = sorted(review_all, key=lambda it: it[1])
        if review_sorted:
            # pass telephely list so dialog can show telephely names when needed
            dlg = ReviewDialog(review_all=review_sorted, telephelyek=self.data.get("telephelyek", []), parent=self)
            dlg.exec()
        else:
            info(self, "Felülvizsgálatok", "Nincsenek felülvizsgálati tételek (dátummal) a kiválasztott szűrés mellett.")

    def _close_active_popup(self) -> None:
        try:
            app = QApplication.instance()
            dlg = app.activeModalWidget()
            if dlg and dlg is not self and isinstance(dlg, QDialog):
                dlg.reject()
                return
            wnd = app.activeWindow()
            if wnd and wnd is not self and isinstance(wnd, QDialog):
                wnd.reject()
        except Exception:
            logger.exception("close active popup failed")

    # ---------------- new handlers: drag&drop and delete shortcut ----------------
    def _on_files_dropped(self, row: int, paths: List[str]) -> None:
        """
        Handle file paths dropped onto row `row` in the table.
        """
        try:
            if row < 0 or row >= self.table.rowCount():
                warning(self, "Csatolmány", "Nem található cél sor a fájlok elhelyezéséhez.")
                return
            item = self.table.item(row, 0)
            if not item:
                warning(self, "Csatolmány", "Nem található engedély a cél sorban.")
                return
            ent = item.data(Qt.UserRole)
            if not ent or not ent.get("id"):
                warning(self, "Csatolmány", "A cél sor nem tartalmaz érvényes engedélyt.")
                return
            # process files
            self._add_files_to_entity(ent, paths)
        except Exception:
            logger.exception("on_files_dropped failed")

    def _delete_selected_via_shortcut(self) -> None:
        try:
            ents = self._get_selected_entities()
            if not ents:
                return
            # reuse bulk delete which asks confirmation
            self._bulk_delete(ents)
        except Exception:
            logger.exception("delete selected via shortcut failed")