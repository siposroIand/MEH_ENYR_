from __future__ import annotations

from typing import Optional, List, Dict, Any
from pathlib import Path
from datetime import datetime, date
import logging
import re

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QCheckBox,
    QDateEdit, QListWidget, QListWidgetItem, QPushButton, QLineEdit,
    QFileDialog, QMessageBox, QWidget, QStackedWidget, QFormLayout, QGroupBox,
    QSplitter, QFrame, QSizePolicy, QTableWidget, QTableWidgetItem, QHeaderView, QTextEdit
)
from PySide6.QtCore import Qt

logger = logging.getLogger(__name__)

# ====================================================================
# IMPORT A EGYSÉGES EXPORTERBŐL (EZ A REFAC)
# ====================================================================
def _import_exporters():
    try:
        from exporters.report_builder_exporter import export_report, ALL_COLUMNS
    except Exception:
        export_report = None
        ALL_COLUMNS = [
            ("telephely", "Telephely"),
            ("kategoria", "Engedélytípus"),
            ("engedely_nev", "Tárgy"),
            ("kiado_hatosag", "Kiadó hatóság"),
            ("hatarozatszam", "Határozat száma"),
            ("lejarat", "Hatálya"),
        ]
    return export_report, ALL_COLUMNS


_DEFAULT_COLUMN_KEYS = [
    "telephely", "kategoria", "engedely_nev", "hatarozatszam", "lejarat"
]

_COLUMN_KEY_NORMALIZATION = {
    "felulvizsgalat_date": "felulvizsgalat",
    "felulvizsgalat": "felulvizsgalat",
}


def _parse_iso_date(value) -> Optional[date]:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        if "T" in value:
            return datetime.fromisoformat(value).date()
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return None


def _safe_filename(s: str) -> str:
    if not s:
        return "unknown"
    return re.sub(r"[^A-Za-z0-9_\-\.]", "_", s).strip("_")


def _compute_group_label(groups_dict: Dict[str, List[Any]], group_by_key: Optional[str], opts: Dict[str, Any], tp_map: Dict[Any, str]) -> str:
    if not group_by_key:
        return ""

    if len(groups_dict) == 1:
        only = next(iter(groups_dict.keys()))
        if group_by_key in ("lejarat_month", "lejarat"):
            try:
                y, m = only.split("-")
                return f" - ({y}/{m})"
            except Exception:
                return f" - ({only})"
        if group_by_key == "lejarat_year":
            return f" - ({only})"
        if group_by_key == "kategoria":
            return f" - ({only})"
        return f" - ({only})"

    tp_ids = opts.get("telephely_ids") or []
    if group_by_key == "telephely" and len(tp_ids) == 1:
        return f" - ({tp_map.get(tp_ids[0], '')})"

    sel_cats = opts.get("selected_categories") or []
    if group_by_key == "kategoria" and len(sel_cats) == 1:
        return f" - ({sel_cats[0]})"

    sel_items = opts.get("selected_items") or []
    if sel_items:
        if group_by_key == "telephely":
            tp_set = {it.get("telephely_id") for it in sel_items if it.get("telephely_id") is not None}
            if len(tp_set) == 1:
                tid = next(iter(tp_set))
                return f" - ({tp_map.get(tid, '')})"
        if group_by_key in ("lejarat_month", "lejarat"):
            ym_set = set()
            for it in sel_items:
                le = _parse_iso_date(it.get("lejarat"))
                if le:
                    ym_set.add(f"{le.year}-{le.month:02d}")
            if len(ym_set) == 1:
                only = next(iter(ym_set))
                y, m = only.split("-")
                return f" - ({y}/{m})"
        if group_by_key == "lejarat_year":
            y_set = set()
            for it in sel_items:
                le = _parse_iso_date(it.get("lejarat"))
                if le:
                    y_set.add(str(le.year))
            if len(y_set) == 1:
                return f" - ({next(iter(y_set))})"

    df = opts.get("date_filter") or {}
    if df.get("enabled"):
        fr = df.get("from"); to = df.get("to")
        if fr and to:
            if group_by_key in ("lejarat_month", "lejarat") and fr.year == to.year and fr.month == to.month:
                return f" - ({fr.year}/{fr.month:02d})"
            if group_by_key == "lejarat_year" and fr.year == to.year:
                return f" - ({fr.year})"

    return ""


class ReportBuilderDialog(QDialog):
    """
    Improved UI: wizard-like step indicator + live preview (table) on the right.
    The logo_path is provided through the constructor (initial["logo_path"]) and there is
    no file-browse control in the dialog by design.
    """
    def __init__(
        self,
        parent=None,
        data: Optional[Dict[str, List[Dict[str, Any]]]] = None,
        telephelyek: Optional[List[Dict[str, Any]]] = None,
        logo_path: Optional[str] = None,
        initial: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Kimutatás készítő")
        self.resize(1100, 760)

        self.data = data or {"engedelyek": []}
        self.telephelyek = telephelyek or []
        self.initial = initial or {}
        if logo_path:
            # keep logo path provided externally; no browsing in the UI
            self.initial.setdefault("logo_path", logo_path)

        # main layout: left wizard stack + right preview
        main = QHBoxLayout(self)

        # left: stacked wizard with step header
        left_frame = QFrame()
        left_layout = QVBoxLayout(left_frame)

        # step header
        self.step_label = QLabel("")
        self.step_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        left_layout.addWidget(self.step_label)

        # stacked pages
        self.stack = QStackedWidget()
        left_layout.addWidget(self.stack)

        # add pages
        self.stack.addWidget(self._page_basic())
        self.stack.addWidget(self._page_filters())
        self.stack.addWidget(self._page_columns())
        self.stack.addWidget(self._page_output())

        # nav buttons (at bottom of left)
        nav = QHBoxLayout()
        self.btn_prev = QPushButton("← Előző")
        self.btn_next = QPushButton("Következő →")
        self.btn_finish = QPushButton("Exportálás")
        self.btn_cancel = QPushButton("Mégse")
        nav.addWidget(self.btn_prev)
        nav.addStretch()
        nav.addWidget(self.btn_cancel)
        nav.addWidget(self.btn_next)
        nav.addWidget(self.btn_finish)
        left_layout.addLayout(nav)

        # right: preview + summary area
        right_frame = QFrame()
        right_layout = QVBoxLayout(right_frame)

        preview_label = QLabel("Előnézet (max 8 sor)")
        preview_label.setStyleSheet("font-weight: bold;")
        right_layout.addWidget(preview_label)

        self.preview_table = QTableWidget(0, 0)
        self.preview_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setWordWrap(True)
        self.preview_table.setAlternatingRowColors(True)
        header = self.preview_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        right_layout.addWidget(self.preview_table, stretch=1)

        # quick summary box
        self.summary_box = QTextEdit()
        self.summary_box.setReadOnly(True)
        self.summary_box.setFixedHeight(120)
        right_layout.addWidget(QLabel("Részletes összefoglaló"))
        right_layout.addWidget(self.summary_box)

        # splitter to allow user resize left/right
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left_frame)
        splitter.addWidget(right_frame)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([700, 400])

        main.addWidget(splitter)

        # connections
        self.btn_prev.clicked.connect(self._prev)
        self.btn_next.clicked.connect(self._next)
        self.btn_finish.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)

        # wire internal updates
        # many controls are created in pages; connect generically using method that re-connects after creation
        self._connect_dynamic_signals()

        # defaults
        self.chk_logo.setChecked(True)

        self._update_step_label()
        self._update_preview_and_summary()

    def _connect_dynamic_signals(self):
        # called at the end of __init__ and can be reused if pages rebuilt
        try:
            self.tp_list.itemChanged.connect(self._on_controls_changed)
            self.category_list.itemChanged.connect(self._on_controls_changed)
            self.chk_include_arch.stateChanged.connect(self._on_controls_changed)
            self.chk_date_filter.stateChanged.connect(self._on_date_filter_toggled)
            self.date_from.dateChanged.connect(self._on_controls_changed)
            self.date_to.dateChanged.connect(self._on_controls_changed)
            self.cmb_group.currentIndexChanged.connect(self._on_controls_changed)
            self.columns_list.itemChanged.connect(self._on_controls_changed)
            # selection change used for move up/down buttons
            self.columns_list.currentRowChanged.connect(self._on_controls_changed)
        except Exception:
            pass

    def _on_controls_changed(self, *a):
        # refresh preview, summary and preview count label
        self._update_preview_and_summary()
        self._update_nav_state()

    def _page_basic(self):
        w = QWidget()
        v = QVBoxLayout(w)

        form = QFormLayout()

        # Title only (no subtitle per request)
        self.doc_title = QLineEdit(
            self.initial.get("title", f"Kimutatás {date.today().isoformat()}")
        )

        # context label under title
        self.lbl_context = QLabel("")
        self.lbl_context.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_context.setWordWrap(True)

        self.cmb_format = QComboBox()
        self.cmb_format.addItems(["PDF", "Excel (XLSX)"])
        self.cmb_format.currentIndexChanged.connect(self._on_controls_changed)

        self.cmb_group = QComboBox()
        self.cmb_group.addItems([
            "Nincs (Összes)",
            "Telephelyenként",
            "Engedélytípusonként",
            "Kiadó hatóság szerint",
            "Lejárat szerint (Év-Hónap)"
            
        ])
        self.cmb_group.currentIndexChanged.connect(self._on_controls_changed)

        self.cmb_file_mode = QComboBox()
        self.cmb_file_mode.addItems([
            "Minden egy fájlban",
            "Csoportonként külön fájl",
        ])

        form.addRow(QLabel("Cím:"), self.doc_title)
        form.addRow(QLabel(""), self.lbl_context)
        form.addRow(QLabel("Kiterjesztés:"), self.cmb_format)
        form.addRow(QLabel("Rendezési szempont:"), self.cmb_group)
        form.addRow(QLabel("Struktúra:"), self.cmb_file_mode)

        box = QGroupBox("Dokumentum beállítások")
        box.setLayout(form)

        note = QLabel("")
        note.setStyleSheet("color: gray; font-size: 11px;")
        v.addWidget(box)
        v.addWidget(note)
        v.addStretch()
        return w

    def _page_filters(self):
        w = QWidget()
        v = QVBoxLayout(w)

        top_row = QHBoxLayout()
        self.chk_include_arch = QCheckBox("Archivált tételek is")
        top_row.addWidget(self.chk_include_arch)
        top_row.addStretch()
        v.addLayout(top_row)

        # telephelyek + select all/none
        v.addWidget(QLabel("Telephelyek (válassz legalább egyet vagy mindet):"))
        tp_row = QHBoxLayout()
        self.tp_list = QListWidget()
        self.tp_list.setSelectionMode(QListWidget.NoSelection)
        for t in self.telephelyek:
            it = QListWidgetItem(t.get("nev", "Ismeretlen"))
            it.setData(Qt.UserRole, t.get("id"))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            self.tp_list.addItem(it)
        tp_btns = QVBoxLayout()
        self.btn_tp_select_all = QPushButton("Összes kijelölése")
        self.btn_tp_select_none = QPushButton("Kijelölés törlése")
        tp_btns.addWidget(self.btn_tp_select_all)
        tp_btns.addWidget(self.btn_tp_select_none)
        tp_btns.addStretch()
        tp_row.addWidget(self.tp_list)
        tp_row.addLayout(tp_btns)
        v.addLayout(tp_row)

        self.btn_tp_select_all.clicked.connect(self._select_all_telephelyes)
        self.btn_tp_select_none.clicked.connect(self._select_none_telephelyes)

        # kategóriák (engedélytípusok) + select all/none
        v.addWidget(QLabel("Engedély-kategóriák:"))
        cat_row = QHBoxLayout()
        self.category_list = QListWidget()
        self.category_list.setSelectionMode(QListWidget.NoSelection)
        cats = sorted({(e.get("kategoria") or "Ismeretlen") for e in self.data.get("engedelyek", [])})
        for c in cats:
            it = QListWidgetItem(c)
            it.setData(Qt.UserRole, c)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            self.category_list.addItem(it)
        cat_btns = QVBoxLayout()
        self.btn_cat_select_all = QPushButton("Összes kijelölése")
        self.btn_cat_select_none = QPushButton("Kijelölés törlése")
        cat_btns.addWidget(self.btn_cat_select_all)
        cat_btns.addWidget(self.btn_cat_select_none)
        cat_btns.addStretch()
        cat_row.addWidget(self.category_list)
        cat_row.addLayout(cat_btns)
        v.addLayout(cat_row)

        self.btn_cat_select_all.clicked.connect(self._select_all_categories)
        self.btn_cat_select_none.clicked.connect(self._select_none_categories)

        # dátum szűrés: bekapcsolható
        date_box = QGroupBox("Lejárat dátum szűrése")
        date_layout = QHBoxLayout()
        self.chk_date_filter = QCheckBox("Szűrés dátumtartomány szerint")
        self.date_from = QDateEdit()
        self.date_from.setCalendarPopup(True)
        self.date_from.setDisplayFormat("yyyy-MM-dd")
        self.date_to = QDateEdit()
        self.date_to.setCalendarPopup(True)
        self.date_to.setDisplayFormat("yyyy-MM-dd")

        self.date_from.setDate(date.today().replace(day=1))
        self.date_to.setDate(date.today())
        self.date_from.setEnabled(False)
        self.date_to.setEnabled(False)

        date_layout.addWidget(self.chk_date_filter)
        date_layout.addWidget(QLabel("Dátumtól:"))
        date_layout.addWidget(self.date_from)
        date_layout.addWidget(QLabel("Dátumig:"))
        date_layout.addWidget(self.date_to)
        date_box.setLayout(date_layout)
        v.addWidget(date_box)

        # preview count
        self.lbl_preview_count = QLabel("")
        v.addWidget(self.lbl_preview_count)

        v.addStretch()
        return w

    def _page_columns(self):
        w = QWidget()
        v = QVBoxLayout(w)

        _, ALL_COLUMNS = _import_exporters()

        self.columns_list = QListWidget()
        # allow drag & drop reordering now so user can change column order
        self.columns_list.setDragDropMode(QListWidget.InternalMove)
        # allow selection so up/down buttons work
        self.columns_list.setSelectionMode(QListWidget.SingleSelection)

        # store mapping key->item for easy lookup (we will respect visual order)
        self._col_items: Dict[str, QListWidgetItem] = {}

        for key, label in ALL_COLUMNS:
            it = QListWidgetItem(label)
            it.setData(Qt.UserRole, key)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            it.setCheckState(Qt.Checked if key in _DEFAULT_COLUMN_KEYS else Qt.Unchecked)
            self.columns_list.addItem(it)
            self._col_items[key] = it

        # controls: select all/none and move up/down
        btn_row = QHBoxLayout()
        left_btns = QHBoxLayout()
        self.btn_select_all = QPushButton("Összes kijelölése")
        self.btn_select_none = QPushButton("Mindet töröl")
        left_btns.addWidget(self.btn_select_all)
        left_btns.addWidget(self.btn_select_none)
        btn_row.addLayout(left_btns)

        right_btns = QVBoxLayout()
        self.btn_move_up = QPushButton("Fel ↑")
        self.btn_move_down = QPushButton("Le ↓")
        right_btns.addWidget(self.btn_move_up)
        right_btns.addWidget(self.btn_move_down)
        right_btns.addStretch()
        btn_row.addLayout(right_btns)
        btn_row.addStretch()

        self.btn_select_all.clicked.connect(self._select_all_columns)
        self.btn_select_none.clicked.connect(self._select_no_columns)
        self.btn_move_up.clicked.connect(self._move_column_up)
        self.btn_move_down.clicked.connect(self._move_column_down)

        v.addWidget(QLabel("Oszlopok kiválasztása és sorrend (húzd vagy használd a Fel/Le gombokat)"))
        v.addLayout(btn_row)
        v.addWidget(self.columns_list)
        v.addStretch()
        return w

    def _page_output(self):
        w = QWidget()
        v = QVBoxLayout(w)

        self.filename_hint = QLineEdit(
            self.initial.get(
                "filename_hint",
                f"kimutatas_{date.today().strftime('%Y%m%d')}"
            )
        )

        # logo checkbox only; path provided by caller
        self.chk_logo = QCheckBox("Logo használata (PDF)")

        v.addWidget(QLabel("Fájlnév"))
        v.addWidget(self.filename_hint)
        v.addWidget(self.chk_logo)
        v.addStretch()
        return w

    def _prev(self):
        idx = max(0, self.stack.currentIndex() - 1)
        self.stack.setCurrentIndex(idx)
        self._update_step_label()
        self._update_preview_and_summary()

    def _next(self):
        idx = min(self.stack.count() - 1, self.stack.currentIndex() + 1)
        self.stack.setCurrentIndex(idx)
        self._update_step_label()
        self._update_preview_and_summary()

    def _update_nav_state(self):
        idx = self.stack.currentIndex()
        self.btn_prev.setEnabled(idx > 0)
        self.btn_next.setEnabled(idx < self.stack.count() - 1)
        self.btn_finish.setVisible(idx == self.stack.count() - 1)

    def _update_step_label(self):
        titles = ["Dokumentum", "Szűrők", "Oszlopok", "Kimenet"]
        idx = self.stack.currentIndex()
        self.step_label.setText(f"{idx+1} / {len(titles)} — {titles[idx]}")

    def _select_all_columns(self):
        for i in range(self.columns_list.count()):
            it = self.columns_list.item(i)
            it.setCheckState(Qt.Checked)
        self._on_controls_changed()

    def _select_no_columns(self):
        for i in range(self.columns_list.count()):
            it = self.columns_list.item(i)
            it.setCheckState(Qt.Unchecked)
        self._on_controls_changed()

    def _select_all_telephelyes(self):
        for i in range(self.tp_list.count()):
            it = self.tp_list.item(i)
            it.setCheckState(Qt.Checked)
        self._on_controls_changed()

    def _select_none_telephelyes(self):
        for i in range(self.tp_list.count()):
            it = self.tp_list.item(i)
            it.setCheckState(Qt.Unchecked)
        self._on_controls_changed()

    def _select_all_categories(self):
        for i in range(self.category_list.count()):
            it = self.category_list.item(i)
            it.setCheckState(Qt.Checked)
        self._on_controls_changed()

    def _select_none_categories(self):
        for i in range(self.category_list.count()):
            it = self.category_list.item(i)
            it.setCheckState(Qt.Unchecked)
        self._on_controls_changed()

    def _on_date_filter_toggled(self, state: int):
        enabled = state == Qt.Checked
        self.date_from.setEnabled(enabled)
        self.date_to.setEnabled(enabled)
        self._on_controls_changed()

    def _move_column_up(self):
        row = self.columns_list.currentRow()
        if row <= 0:
            return
        item = self.columns_list.takeItem(row)
        # re-insert at row-1
        self.columns_list.insertItem(row - 1, item)
        self.columns_list.setCurrentRow(row - 1)
        self._on_controls_changed()

    def _move_column_down(self):
        row = self.columns_list.currentRow()
        if row < 0 or row >= self.columns_list.count() - 1:
            return
        item = self.columns_list.takeItem(row)
        self.columns_list.insertItem(row + 1, item)
        self.columns_list.setCurrentRow(row + 1)
        self._on_controls_changed()

    def get_options(self) -> Dict[str, Any]:
        # Respect visual order from columns_list; only include keys that are checked
        columns = []
        for i in range(self.columns_list.count()):
            it = self.columns_list.item(i)
            if it.checkState() == Qt.Checked:
                columns.append(it.data(Qt.UserRole))

        columns = [_COLUMN_KEY_NORMALIZATION.get(c, c) for c in columns]

        group_map = {
            "Telephelyenként": "telephely",
            "Engedélytípusonként": "kategoria",
            "Kiadó hatóság szerint": "kiado_hatosag",
            "Lejárat szerint (Év-Hónap)": "lejarat_month",
            "Nincs (Összes)": None
        }

        date_filter_enabled = self.chk_date_filter.isChecked()
        date_from = None
        date_to = None
        if date_filter_enabled:
            try:
                date_from = self.date_from.date().toPython()
                date_to = self.date_to.date().toPython()
            except Exception:
                date_from = None
                date_to = None

        telephely_ids = [
            self.tp_list.item(i).data(Qt.UserRole)
            for i in range(self.tp_list.count())
            if self.tp_list.item(i).checkState() == Qt.Checked
        ]

        selected_categories = [
            self.category_list.item(i).data(Qt.UserRole)
            for i in range(self.category_list.count())
            if self.category_list.item(i).checkState() == Qt.Checked
        ]

        # logo_path comes from initial (no browse in UI)
        logo_path = self.initial.get("logo_path", "")

        return {
            "format": "PDF" if self.cmb_format.currentText().startswith("PDF") else "XLSX",
            "group_by": group_map.get(self.cmb_group.currentText()),
            "group_by_display": self.cmb_group.currentText(),
            "per_group": self.cmb_file_mode.currentIndex() == 1,
            "include_archived": self.chk_include_arch.isChecked(),
            "telephely_ids": telephely_ids,
            "columns": columns,
            "filename_hint": _safe_filename(self.filename_hint.text() or ""),
            "title": (self.doc_title.text() or ""),
            # subtitle intentionally omitted
            "include_logo": self.chk_logo.isChecked(),
            "logo_path": (logo_path or ""),
            "date_format": "%Y-%m-%d",
            "date_filter": {
                "enabled": date_filter_enabled,
                "from": date_from,
                "to": date_to,
            },
            "selected_categories": selected_categories,
            "selected_items": [self._get_item_by_row(i) for i in range(self.category_list.count())],
        }

    def _get_item_by_row(self, idx: int) -> Dict[str, Any]:
        # placeholder to keep compatibility; real selection inference uses selected_categories
        return {}

    def _update_preview_and_summary(self):
        # refresh preview table and summary box based on current options
        opts = self.get_options()
        # compute filtered items (same logic as run_report)
        items = list(self.data.get("engedelyek", []))
        filtered = []
        for e in items:
            if not opts.get("include_archived") and e.get("archived"):
                continue
            tp_ids = opts.get("telephely_ids") or []
            if tp_ids and (e.get("telephely_id") not in tp_ids):
                continue
            df = opts.get("date_filter") or {}
            if df.get("enabled"):
                lejar = _parse_iso_date(e.get("lejarat"))
                if not lejar:
                    continue
                from_d = df.get("from")
                to_d = df.get("to")
                if from_d and lejar < from_d:
                    continue
                if to_d and lejar > to_d:
                    continue
            sel_cats = opts.get("selected_categories") or []
            if sel_cats and (e.get("kategoria") not in sel_cats):
                continue
            filtered.append(e)

        # update preview count label
        self.lbl_preview_count.setText(f"Szűrésnek megfelelő tételek: {len(filtered)}")

        # prepare preview table columns and rows
        _, ALL_COLUMNS = _import_exporters()
        key_to_label = {k: l for k, l in ALL_COLUMNS}
        columns = opts.get("columns") or [k for k, _ in ALL_COLUMNS]
        # prune using exporter logic is left to exporter; preview shows chosen columns in visual order
        self.preview_table.clear()
        cols = [key_to_label.get(k, k) for k in columns]
        self.preview_table.setColumnCount(len(cols))
        self.preview_table.setHorizontalHeaderLabels(cols)

        # take up to 8 rows
        preview_rows = filtered[:8]
        self.preview_table.setRowCount(len(preview_rows))
        tp_map = {t["id"]: t.get("nev", "Ismeretlen") for t in self.telephelyek}

        for r_idx, e in enumerate(preview_rows):
            for c_idx, key in enumerate(columns):
                # format values similar to exporter: None -> ""
                val = e.get(key)
                if key == "telephely":
                    val = tp_map.get(e.get("telephely_id"), "")
                elif key in ("lejarat", "lejarat_display"):
                    # show full original date (YYYY.MM.DD) in preview too
                    if val is None:
                        val = ""
                    else:
                        try:
                            if isinstance(val, date):
                                val = val.strftime("%Y.%m.%d")
                            elif isinstance(val, str) and "T" in val:
                                val = datetime.fromisoformat(val).date().strftime("%Y.%m.%d")
                            else:
                                val = datetime.strptime(val, "%Y-%m-%d").date().strftime("%Y.%m.%d")
                        except Exception:
                            val = str(val) if val is not None else ""
                elif key == "modosito_hat" and val is not None:
                    # convert semicolons to newlines for preview
                    val = re.sub(r";\s*", "\n", (val if isinstance(val, str) else str(val)))
                else:
                    val = "" if val is None else val
                item = QTableWidgetItem(str(val))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setToolTip(str(val))
                self.preview_table.setItem(r_idx, c_idx, item)

        # autosize columns but avoid exceeding width: let header be interactive but we auto-resize to contents then cap
        self.preview_table.resizeColumnsToContents()
        total_w = sum(self.preview_table.columnWidth(i) for i in range(self.preview_table.columnCount()))
        available = self.preview_table.viewport().width() or 600
        if total_w > available and self.preview_table.columnCount() > 0:
            # scale down proportionally
            for i in range(self.preview_table.columnCount()):
                w = self.preview_table.columnWidth(i)
                new_w = max(60, int(w * available / total_w))
                self.preview_table.setColumnWidth(i, new_w)
        # ensure last section stretches to fill
        self.preview_table.horizontalHeader().setStretchLastSection(True)

        # summary text
        s_lines = [
            f"Cím: {opts.get('title') or '(nincs)'}",
            f"Kiterjesztés: {opts.get('format')}",
            f"Rendezési szempont: {opts.get('group_by_display')}",
            f"Struktúra: {'Csoportonként' if opts.get('per_group') else 'Egy fájl'}",
            f"Logó használata: {'Igen' if opts.get('include_logo') else 'Nem'}",
            f"Tételszám: {len(filtered)}"
        ]
        s_lines.append("Kijelölt oszlopok: " + ", ".join([c for c in columns]) if columns else "Kijelölt oszlopok: (nincs)")
        self.summary_box.setPlainText("\n".join(s_lines))

        self._update_step_label()
        self._update_nav_state()

    def _choose_logo(self):
        # browsing removed as requested
        return

# ====================================================================
# RUN_REPORT (egyesített export hívás az új exporterrel)
# ====================================================================
def run_report(
    parent,
    opts: Dict[str, Any],
    data: Dict[str, List[Dict[str, Any]]],
    telephelyek: List[Dict[str, Any]],
    logo_path: Optional[str] = None,
    attachments_dir: Optional[str] = None,
):
    # unchanged: delegates to exporter
    from PySide6.QtWidgets import QMessageBox
    export_report, _ = _import_exporters()

    items = list(data.get("engedelyek", []))
    if not items:
        QMessageBox.information(parent, "Kimutatás", "Nincs exportálható adat.")
        return

    # same filtering logic as before
    def _item_in_filters(e: Dict[str, Any]) -> bool:
        if not opts.get("include_archived") and e.get("archived"):
            return False
        tp_ids = opts.get("telephely_ids") or []
        if tp_ids and (e.get("telephely_id") not in tp_ids):
            return False
        df = opts.get("date_filter") or {}
        if df.get("enabled"):
            lejar = _parse_iso_date(e.get("lejarat"))
            if not lejar:
                return False
            from_d = df.get("from")
            to_d = df.get("to")
            if from_d and lejar < from_d:
                return False
            if to_d and lejar > to_d:
                return False
        return True

    filtered_items = [e for e in items if _item_in_filters(e)]
    if not filtered_items:
        QMessageBox.information(parent, "Kimutatás", "A szűrésnek nincs megfelelő tétel.")
        return

    sel_cats = opts.get("selected_categories") or []
    if sel_cats:
        filtered_items = [e for e in filtered_items if (e.get("kategoria") or "Ismeretlen") in sel_cats]

    if not filtered_items:
        QMessageBox.information(parent, "Kimutatás", "Nincsenek kiválasztott tételek az aktuális szűrés mellett.")
        return

    # grouping and call to exporter unchanged (exporter takes care of details)
    fmt = opts.get("format", "PDF")
    export_fn, _ = _import_exporters()
    if not export_fn:
        QMessageBox.critical(parent, "Export", "Exporter modul nem érhető el.")
        return

    # Prepare groups (same logic as prior implementation)
    group_by = opts.get("group_by")
    groups: Dict[str, List[Dict[str, Any]]] = {}
    tp_map = {t["id"]: t.get("nev", "Ismeretlen") for t in telephelyek}

    if group_by == "telephely":
        for e in filtered_items:
            groups.setdefault(tp_map.get(e.get("telephely_id"), "Ismeretlen"), []).append(e)
    elif group_by == "kategoria":
        for e in filtered_items:
            groups.setdefault(e.get("kategoria") or "Ismeretlen", []).append(e)
    elif group_by == "status":
        today = date.today()
        for e in filtered_items:
            ht = (e.get("hataly_tipus") or "")
            try:
                ht = ht.strip().lower()
            except Exception:
                ht = str(ht).lower() if ht is not None else ""
            if ht == "fixed":
                lejar = _parse_iso_date(e.get("lejarat"))
                key = "Aktív" if (lejar and lejar >= today) else "Lejárt"
            else:
                key = "Aktív"
            groups.setdefault(key, []).append(e)
    elif group_by == "kiado_hatosag":
        for e in filtered_items:
            groups.setdefault(e.get("kiado_hatosag") or "Ismeretlen", []).append(e)
    elif group_by == "lejarat_month":
        for e in filtered_items:
            lejar = _parse_iso_date(e.get("lejarat"))
            if lejar:
                key = lejar.strftime("%Y-%m")
            else:
                key = "Ismeretlen"
            groups.setdefault(key, []).append(e)
    elif group_by == "lejarat_year":
        for e in filtered_items:
            lejar = _parse_iso_date(e.get("lejarat"))
            if lejar:
                key = str(lejar.year)
            else:
                key = "Ismeretlen"
            groups.setdefault(key, []).append(e)
    else:
        groups["Összes"] = filtered_items

    filename_hint = opts.get("filename_hint") or "kimutatas"
    per_group = opts.get("per_group", False)
    columns = opts.get("columns") or []

    # call exporter (kept simple)
    if fmt == "PDF":
        if per_group:
            out_dir = QFileDialog.getExistingDirectory(parent, "Mappa kiválasztása a csoportonkénti mentéshez")
            if not out_dir:
                return
            try:
                export_fn(out_dir, groups, telephelyek, columns, fmt="PDF", title=(opts.get("title") or ""), logo_path=(opts.get("logo_path")), include_waste=True, per_group=True)
            except Exception as exc:
                logger.exception("Export per-group PDF error: %s", exc)
                QMessageBox.critical(parent, "Export", f"Hiba az exportálás során: {exc}")
                return
            QMessageBox.information(parent, "Export", "PDF fájlok elkészültek.")
            return
        else:
            path, _ = QFileDialog.getSaveFileName(parent, "PDF mentése", f"{filename_hint}.pdf", "PDF (*.pdf)")
            if not path:
                return
            try:
                export_fn(path, groups, telephelyek, columns, fmt="PDF", title=(opts.get("title") or ""), logo_path=(opts.get("logo_path")), include_waste=True, per_group=False)
            except Exception as exc:
                logger.exception("PDF export hiba: %s", exc)
                QMessageBox.critical(parent, "Export", f"Hiba a PDF mentésekor: {exc}")
                return
            QMessageBox.information(parent, "Export", "PDF elkészült.")
            return
    else:
        if per_group:
            out_dir = QFileDialog.getExistingDirectory(parent, "Mappa kiválasztása a csoportonkénti mentéshez")
            if not out_dir:
                return
            try:
                export_fn(out_dir, groups, telephelyek, columns, fmt="XLSX", title=(opts.get("title") or ""), include_waste=True, per_group=True)
            except Exception as exc:
                logger.exception("Excel per-group export hiba: %s", exc)
                QMessageBox.critical(parent, "Export", f"Hiba az exportálás során: {exc}")
                return
            QMessageBox.information(parent, "Export", "Excel fájlok elkészültek.")
            return
        else:
            path, _ = QFileDialog.getSaveFileName(parent, "Excel mentése", f"{filename_hint}.xlsx", "Excel (*.xlsx)")
            if not path:
                return
            try:
                export_fn(path, groups, telephelyek, columns, fmt="XLSX", title=(opts.get("title") or ""), include_waste=True, per_group=False)
            except Exception as exc:
                logger.exception("Excel export hiba: %s", exc)
                QMessageBox.critical(parent, "Export", f"Hiba az Excel mentésekor: {exc}")
                return
            QMessageBox.information(parent, "Export", "Excel elkészült.")
            return