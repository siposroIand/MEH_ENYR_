from __future__ import annotations

from datetime import date
import logging
from pathlib import Path
import re
import sqlite3

from PySide6.QtCore import Qt, QStandardPaths
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QCheckBox,
    QDateEdit, QListWidget, QListWidgetItem, QPushButton, QLineEdit,
    QFileDialog, QMessageBox, QWidget, QStackedWidget, QFormLayout,
    QSplitter, QFrame, QTableWidget, QTableWidgetItem, QHeaderView,
    QTextEdit, QInputDialog, QScrollArea, QAbstractItemView,
)

from config.app_config import APP_NAME
from core.report_data import GROUPS, VALIDITY_STATES, UNKNOWN, build_report_groups, parse_date
from core.report_templates import ReportTemplateStore, validate_options
from exporters.report_builder_exporter import ALL_COLUMNS, ALIASES, export_report, prepare_report_groups

logger = logging.getLogger(__name__)
DEFAULT_COLUMNS = ["telephely", "kategoria", "engedely_nev", "hatarozatszam", "lejarat"]
PREVIEW_LIMIT = 8
STORE_ERRORS = (OSError, sqlite3.Error, ValueError)


def _safe_filename(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .") or "kimutatas"


class ReportBuilderDialog(QDialog):
    def __init__(self, parent=None, data=None, telephelyek=None, logo_path=None,
                 initial=None, template_store=None):
        super().__init__(parent)
        self.setWindowTitle("Kimutatáskészítő")
        self.resize(1150, 760)
        self.data = data or {"engedelyek": []}
        self.telephelyek = telephelyek or []
        self.initial = dict(initial or {})
        self.logo_path = logo_path or self.initial.get("logo_path") or ""
        root = Path(QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation)) / APP_NAME
        self.template_store = template_store or ReportTemplateStore(root / "report_templates.sqlite3")
        self._updating = True
        self._prepared = []
        self._preview_error = ""

        layout = QVBoxLayout(self)
        template_row = QHBoxLayout()
        template_row.addWidget(QLabel("Mentett sablon:"))
        self.template_combo = QComboBox()
        self.template_combo.setMinimumContentsLength(18)
        template_row.addWidget(self.template_combo, 1)
        self.btn_template_load = QPushButton("Betöltés")
        self.btn_template_save = QPushButton("Mentés sablonként…")
        self.btn_template_delete = QPushButton("Törlés")
        for button in (self.btn_template_load, self.btn_template_save, self.btn_template_delete):
            template_row.addWidget(button)
        layout.addLayout(template_row)
        self.template_status = QLabel("")
        self.template_status.setWordWrap(True)
        layout.addWidget(self.template_status)

        left = QFrame()
        left_layout = QVBoxLayout(left)
        self.step_label = QLabel()
        self.step_label.setStyleSheet("font-weight: bold")
        left_layout.addWidget(self.step_label)
        self.stack = QStackedWidget()
        for page in (self._page_basic(), self._page_filters(), self._page_columns(), self._page_output()):
            self.stack.addWidget(page)
        left_layout.addWidget(self.stack)
        nav = QHBoxLayout()
        self.btn_prev = QPushButton("← Előző")
        self.btn_next = QPushButton("Következő →")
        self.btn_finish = QPushButton("Exportálás")
        self.btn_cancel = QPushButton("Mégse")
        for button in (self.btn_prev, self.btn_cancel, self.btn_next, self.btn_finish):
            nav.addWidget(button)
        left_layout.addLayout(nav)

        right = QFrame()
        right_layout = QVBoxLayout(right)
        self.preview_label = QLabel()
        self.preview_label.setWordWrap(True)
        right_layout.addWidget(self.preview_label)
        self.preview_group = QComboBox()
        self.preview_group.setToolTip("Az exportban külön munkalapra / oldalra kerülő csoport.")
        right_layout.addWidget(self.preview_group)
        self.preview_table = QTableWidget()
        self.preview_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.preview_table.setWordWrap(True)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        right_layout.addWidget(self.preview_table, 1)
        self.summary_box = QTextEdit()
        self.summary_box.setReadOnly(True)
        self.summary_box.setMaximumHeight(160)
        right_layout.addWidget(self.summary_box)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([560, 590])
        layout.addWidget(splitter, 1)

        self._connect_signals()
        self._apply_options(self.initial)
        self._refresh_templates()

    def _page_basic(self):
        page = QWidget()
        form = QFormLayout(page)
        self.doc_title = QLineEdit(f"Kimutatás {date.today().isoformat()}")
        self.cmb_format = QComboBox()
        self.cmb_format.addItem("PDF", "PDF")
        self.cmb_format.addItem("Excel (XLSX)", "XLSX")
        self.cmb_group = QComboBox()
        for key, label in GROUPS:
            self.cmb_group.addItem(label, key)
        self.cmb_file_mode = QComboBox()
        self.cmb_file_mode.addItems(["Minden egy fájlban", "Csoportonként külön fájl"])
        form.addRow("Cím:", self.doc_title)
        form.addRow("Formátum:", self.cmb_format)
        form.addRow("Csoportosítás:", self.cmb_group)
        form.addRow("Struktúra:", self.cmb_file_mode)
        note = QLabel("Csoporton belül engedélytípus szerint rendezve. "
                      "Excelben csoportonként munkalap, PDF-ben új oldal készül.")
        note.setWordWrap(True)
        form.addRow(note)
        return page

    @staticmethod
    def _add_check_item(widget, label, value, checked=True):
        item = QListWidgetItem(str(label))
        item.setData(Qt.UserRole, value)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        widget.addItem(item)

    def _filter_list(self, layout, title, values):
        all_check = QCheckBox(f"{title}: mindegyik")
        all_check.setChecked(True)
        widget = QListWidget()
        widget.setMaximumHeight(95)
        widget.setSelectionMode(QAbstractItemView.NoSelection)
        for value, label in values:
            self._add_check_item(widget, label, value)
        layout.addWidget(all_check)
        layout.addWidget(widget)
        row = QHBoxLayout()
        for label, checked in (("Összes kijelölése", True), ("Kijelölés törlése", False)):
            button = QPushButton(label)
            button.clicked.connect(lambda _, w=widget, a=all_check, c=checked: self._select_filter(w, a, c))
            row.addWidget(button)
        layout.addLayout(row)
        all_check.toggled.connect(lambda enabled: widget.setEnabled(not enabled))
        widget.setEnabled(False)
        return all_check, widget

    def _page_filters(self):
        content = QWidget()
        layout = QVBoxLayout(content)
        self.chk_include_arch = QCheckBox("Archivált tételek is")
        layout.addWidget(self.chk_include_arch)
        self.chk_all_sites, self.tp_list = self._filter_list(
            layout, "Telephelyek", [(t["id"], t.get("nev") or UNKNOWN) for t in self.telephelyek])
        permits = self.data.get("engedelyek", [])
        categories = sorted({e.get("kategoria") or UNKNOWN for e in permits}, key=str.casefold)
        self.chk_all_categories, self.category_list = self._filter_list(
            layout, "Engedélytípusok", [(c, c) for c in categories])
        authorities = sorted({e.get("kiado_hatosag") or UNKNOWN for e in permits}, key=str.casefold)
        self.chk_all_authorities, self.authority_list = self._filter_list(
            layout, "Kiadó hatóságok", [(a, a) for a in authorities])
        form = QFormLayout()
        self.text_filter = QLineEdit()
        self.text_filter.setPlaceholderText("Tárgy, határozatszám, hatóság, megjegyzés, telephely…")
        self.validity_filter = QComboBox()
        for key, label in VALIDITY_STATES:
            self.validity_filter.addItem(label, key)
        form.addRow("Szövegrészlet:", self.text_filter)
        form.addRow("Érvényesség:", self.validity_filter)
        layout.addLayout(form)
        self.chk_date_filter = QCheckBox("Lejárat szűrése dátumtartomány szerint (határokkal együtt)")
        layout.addWidget(self.chk_date_filter)
        dates = QHBoxLayout()
        self.date_from = QDateEdit(date.today().replace(day=1))
        self.date_to = QDateEdit(date.today())
        for widget in (self.date_from, self.date_to):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd")
            widget.setEnabled(False)
        dates.addWidget(QLabel("Dátumtól:"))
        dates.addWidget(self.date_from)
        dates.addWidget(QLabel("Dátumig:"))
        dates.addWidget(self.date_to)
        layout.addLayout(dates)
        self.lbl_preview_count = QLabel()
        layout.addWidget(self.lbl_preview_count)
        note = QLabel("A „mindegyik” kikapcsolása után csak a bejelölt értékek szerepelnek. "
                      "Üres kijelölés: nincs találat. Törölt tételek soha nem kerülnek a kimutatásba.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        return scroll

    def _page_columns(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel("Oszlopok és sorrend: húzza a sorokat, vagy használja a Fel/Le gombokat. "
                       "A kiválasztott üres oszlopok is megmaradnak.")
        label.setWordWrap(True)
        layout.addWidget(label)
        self.columns_list = QListWidget()
        self.columns_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.columns_list.setDefaultDropAction(Qt.MoveAction)
        self.columns_list.setSelectionMode(QAbstractItemView.SingleSelection)
        for key, label in ALL_COLUMNS:
            self._add_check_item(self.columns_list, label, key, key in DEFAULT_COLUMNS)
        layout.addWidget(self.columns_list)
        row = QHBoxLayout()
        self.btn_select_all = QPushButton("Összes kijelölése")
        self.btn_select_none = QPushButton("Kijelölés törlése")
        self.btn_move_up = QPushButton("Fel ↑")
        self.btn_move_down = QPushButton("Le ↓")
        for button in (self.btn_select_all, self.btn_select_none, self.btn_move_up, self.btn_move_down):
            row.addWidget(button)
        layout.addLayout(row)
        return page

    def _page_output(self):
        page = QWidget()
        form = QFormLayout(page)
        self.filename_hint = QLineEdit(f"kimutatas_{date.today():%Y%m%d}")
        self.chk_logo = QCheckBox("Logó használata (PDF)")
        self.chk_logo.setChecked(True)
        form.addRow("Fájlnév:", self.filename_hint)
        form.addRow(self.chk_logo)
        note = QLabel("Az előnézet legfeljebb 8 sort mutat csoportonként. "
                      "Az export minden szűrésnek megfelelő sort tartalmaz.")
        note.setWordWrap(True)
        form.addRow(note)
        return page

    def _connect_signals(self):
        for widget in (self.doc_title, self.filename_hint, self.text_filter):
            widget.textChanged.connect(self._on_controls_changed)
        for widget in (self.cmb_format, self.cmb_group, self.cmb_file_mode, self.validity_filter):
            widget.currentIndexChanged.connect(self._on_controls_changed)
        for widget in (self.chk_include_arch, self.chk_logo, self.chk_all_sites,
                       self.chk_all_categories, self.chk_all_authorities):
            widget.toggled.connect(self._on_controls_changed)
        for widget in (self.tp_list, self.category_list, self.authority_list, self.columns_list):
            widget.itemChanged.connect(self._on_controls_changed)
        self.columns_list.model().rowsMoved.connect(self._on_controls_changed)
        self.columns_list.currentRowChanged.connect(self._update_nav_state)
        self.chk_date_filter.toggled.connect(self._on_date_filter_toggled)
        self.date_from.dateChanged.connect(self._on_controls_changed)
        self.date_to.dateChanged.connect(self._on_controls_changed)
        self.btn_select_all.clicked.connect(lambda: self._select_columns(True))
        self.btn_select_none.clicked.connect(lambda: self._select_columns(False))
        self.btn_move_up.clicked.connect(self._move_column_up)
        self.btn_move_down.clicked.connect(self._move_column_down)
        self.btn_prev.clicked.connect(lambda: self._navigate(-1))
        self.btn_next.clicked.connect(lambda: self._navigate(1))
        self.btn_finish.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        self.preview_group.currentIndexChanged.connect(self._render_preview)
        self.btn_template_save.clicked.connect(self._save_template)
        self.btn_template_load.clicked.connect(self._load_template)
        self.btn_template_delete.clicked.connect(self._delete_template)

    @staticmethod
    def _checked(widget):
        return [widget.item(i).data(Qt.UserRole) for i in range(widget.count())
                if widget.item(i).checkState() == Qt.Checked]

    def get_options(self):
        return {
            "title": self.doc_title.text(), "format": self.cmb_format.currentData(),
            "group_by": self.cmb_group.currentData(), "group_by_display": self.cmb_group.currentText(),
            "per_group": self.cmb_file_mode.currentIndex() == 1,
            "include_archived": self.chk_include_arch.isChecked(),
            "site_filter_enabled": not self.chk_all_sites.isChecked(),
            "telephely_ids": self._checked(self.tp_list) if not self.chk_all_sites.isChecked() else [],
            "selected_categories": None if self.chk_all_categories.isChecked() else self._checked(self.category_list),
            "selected_authorities": None if self.chk_all_authorities.isChecked() else self._checked(self.authority_list),
            "text_filter": self.text_filter.text(), "validity_state": self.validity_filter.currentData(),
            "columns": self._checked(self.columns_list),
            "column_order": [self.columns_list.item(i).data(Qt.UserRole) for i in range(self.columns_list.count())],
            "filename_hint": _safe_filename(self.filename_hint.text()),
            "include_logo": self.chk_logo.isChecked(), "logo_path": self.logo_path,
            "date_filter": {
                "enabled": self.chk_date_filter.isChecked(),
                "from": self.date_from.date().toPython(), "to": self.date_to.date().toPython(),
            },
        }

    def _apply_selection(self, widget, all_check, values):
        all_check.setChecked(values is None)
        existing = [widget.item(i).data(Qt.UserRole) for i in range(widget.count())]
        for value in values or []:
            if value not in existing:
                self._add_check_item(widget, f"{value} (jelenleg nem elérhető)", value)
        for i in range(widget.count()):
            item = widget.item(i)
            item.setCheckState(Qt.Checked if values is None or item.data(Qt.UserRole) in values else Qt.Unchecked)

    def _apply_options(self, options):
        options = validate_options(options)
        columns = [ALIASES.get(c, c) for c in options.get("columns", DEFAULT_COLUMNS)]
        order = [ALIASES.get(c, c) for c in options.get("column_order", columns)]
        known = dict(ALL_COLUMNS)
        if any(c not in known for c in columns + order):
            raise ValueError("A sablon ismeretlen oszlopot tartalmaz.")
        order = list(dict.fromkeys(order + columns + list(known)))
        self._updating = True
        try:
            self.doc_title.setText(options.get("title", f"Kimutatás {date.today().isoformat()}"))
            self.cmb_format.setCurrentIndex(self.cmb_format.findData(options.get("format", "PDF")))
            self.cmb_group.setCurrentIndex(self.cmb_group.findData(options.get("group_by")))
            self.cmb_file_mode.setCurrentIndex(int(options.get("per_group", False)))
            self.chk_include_arch.setChecked(options.get("include_archived", False))
            sites = options.get("telephely_ids", [])
            sites_enabled = options.get("site_filter_enabled", bool(sites))
            self._apply_selection(self.tp_list, self.chk_all_sites, sites if sites_enabled else None)
            self._apply_selection(self.category_list, self.chk_all_categories, options.get("selected_categories"))
            self._apply_selection(self.authority_list, self.chk_all_authorities, options.get("selected_authorities"))
            self.text_filter.setText(options.get("text_filter", ""))
            self.validity_filter.setCurrentIndex(self.validity_filter.findData(options.get("validity_state", "all")))
            df = options.get("date_filter", {})
            self.date_from.setDate(parse_date(df.get("from")) or date.today().replace(day=1))
            self.date_to.setDate(parse_date(df.get("to")) or date.today())
            self.chk_date_filter.setChecked(df.get("enabled", False))
            self.columns_list.clear()
            for key in order:
                self._add_check_item(self.columns_list, known[key], key, key in columns)
            self.filename_hint.setText(options.get("filename_hint", f"kimutatas_{date.today():%Y%m%d}"))
            self.chk_logo.setChecked(options.get("include_logo", True))
        finally:
            self._updating = False
        self._on_controls_changed()

    def _select_filter(self, widget, all_check, checked):
        self._updating = True
        all_check.setChecked(False)
        for i in range(widget.count()):
            widget.item(i).setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._updating = False
        self._on_controls_changed()

    def _select_columns(self, checked):
        self._updating = True
        for i in range(self.columns_list.count()):
            self.columns_list.item(i).setCheckState(Qt.Checked if checked else Qt.Unchecked)
        self._updating = False
        self._on_controls_changed()

    def _move_column(self, offset):
        index = self.columns_list.currentRow()
        if index < 0 or not 0 <= index + offset < self.columns_list.count():
            return
        item = self.columns_list.takeItem(index)
        self.columns_list.insertItem(index + offset, item)
        self.columns_list.setCurrentRow(index + offset)
        self._on_controls_changed()

    def _move_column_up(self):
        self._move_column(-1)

    def _move_column_down(self):
        self._move_column(1)

    def _on_date_filter_toggled(self, enabled):
        self.date_from.setEnabled(enabled)
        self.date_to.setEnabled(enabled)
        self._on_controls_changed()

    def _navigate(self, offset):
        self.stack.setCurrentIndex(max(0, min(3, self.stack.currentIndex() + offset)))
        self._update_nav_state()

    def _update_nav_state(self, *_):
        if self._updating:
            return
        index = self.stack.currentIndex()
        self.step_label.setText(f"{index+1} / 4 — {['Dokumentum', 'Szűrők', 'Oszlopok', 'Kimenet'][index]}")
        self.btn_prev.setEnabled(index > 0)
        self.btn_next.setVisible(index < 3)
        self.btn_finish.setVisible(index == 3)
        self.btn_finish.setEnabled(not self._preview_error and any(g.rows for g in self._prepared))
        row = self.columns_list.currentRow()
        self.btn_move_up.setEnabled(row > 0)
        self.btn_move_down.setEnabled(0 <= row < self.columns_list.count()-1)

    def _on_controls_changed(self, *_):
        if not self._updating:
            self._update_preview_and_summary()

    def _update_preview_and_summary(self):
        opts = self.get_options()
        self._preview_error = ""
        try:
            if not opts["columns"]:
                raise ValueError("Válasszon legalább egy oszlopot.")
            groups = build_report_groups(self.data.get("engedelyek", []), self.telephelyek, opts)
            self._prepared = prepare_report_groups(groups, self.telephelyek, opts["columns"], prune_empty=False)
        except ValueError as exc:
            self._prepared = []
            self._preview_error = str(exc)
        current = self.preview_group.currentText()
        self.preview_group.blockSignals(True)
        self.preview_group.clear()
        self.preview_group.addItems([group.name for group in self._prepared])
        index = self.preview_group.findText(current)
        if index >= 0:
            self.preview_group.setCurrentIndex(index)
        self.preview_group.blockSignals(False)
        count = sum(len(group.rows) for group in self._prepared)
        self.lbl_preview_count.setText(self._preview_error or f"Szűrésnek megfelelő tételek: {count}")
        labels = dict(ALL_COLUMNS)
        self.summary_box.setPlainText(
            f"Cím: {opts['title'] or '(nincs)'}\nFormátum: {opts['format']}\n"
            f"Csoportosítás: {self.cmb_group.currentText()}\n"
            f"Struktúra: {self.cmb_file_mode.currentText()}\n"
            f"Oszlopok: {', '.join(labels[c] for c in opts['columns'])}\n"
            f"Tételszám: {count}; csoportok: {len(self._prepared)}"
        )
        self._render_preview()
        self._update_nav_state()

    def _render_preview(self, *_):
        self.preview_table.clear()
        index = self.preview_group.currentIndex()
        group = self._prepared[index] if 0 <= index < len(self._prepared) else None
        total = sum(len(g.rows) for g in self._prepared)
        if group is None:
            self.preview_table.setRowCount(0)
            self.preview_table.setColumnCount(0)
            self.preview_label.setText(self._preview_error or "Nincs a szűrésnek megfelelő tétel.")
            return
        rows = group.rows[:PREVIEW_LIMIT]
        self.preview_label.setText(
            f"Előnézet: {len(rows)} / {len(group.rows)} sor ebben a csoportban (korlát: {PREVIEW_LIMIT}). "
            f"Teljes export: {total} sor, {len(self._prepared)} csoport. Az export minden sort tartalmaz."
        )
        self.preview_table.setColumnCount(len(group.columns))
        self.preview_table.setHorizontalHeaderLabels(group.headers)
        self.preview_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                self.preview_table.setItem(row_index, col_index, item)
        self.preview_table.resizeColumnsToContents()
        for i in range(self.preview_table.columnCount()):
            self.preview_table.setColumnWidth(i, min(260, max(90, self.preview_table.columnWidth(i))))
        self.preview_table.resizeRowsToContents()

    def accept(self):
        self._update_preview_and_summary()
        if self._preview_error or not any(g.rows for g in self._prepared):
            QMessageBox.warning(self, "Kimutatás", self._preview_error or "Nincs exportálható tétel.")
            return
        super().accept()

    def _refresh_templates(self, selected=None):
        try:
            names = self.template_store.names()
        except STORE_ERRORS as exc:
            self.template_status.setText(f"A sablonok nem olvashatók: {exc}")
            self.btn_template_load.setEnabled(False)
            self.btn_template_delete.setEnabled(False)
            return
        self.template_combo.clear()
        self.template_combo.addItems(names)
        if selected in names:
            self.template_combo.setCurrentText(selected)
        self.btn_template_load.setEnabled(bool(names))
        self.btn_template_delete.setEnabled(bool(names))

    def _save_template(self):
        name, ok = QInputDialog.getText(self, "Sablon mentése", "Sablonnév:", text=self.template_combo.currentText())
        if not ok:
            return
        name = name.strip()
        try:
            exists = name in self.template_store.names()
            if exists and QMessageBox.question(self, "Sablon felülírása", f"Felülírja a(z) „{name}” sablont?",
                                               QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            self.template_store.save(name, self.get_options(), overwrite=exists)
        except STORE_ERRORS as exc:
            QMessageBox.warning(self, "Sablon mentési hiba", str(exc))
            return
        self.template_status.setText(f"Sablon mentve: {name}")
        self._refresh_templates(name)

    def _load_template(self):
        name = self.template_combo.currentText()
        if not name:
            return
        try:
            self._apply_options(self.template_store.load(name))
        except STORE_ERRORS as exc:
            QMessageBox.warning(self, "Sablon betöltési hiba", str(exc))
            return
        self.template_status.setText(f"Sablon betöltve: {name}")

    def _delete_template(self):
        name = self.template_combo.currentText()
        if not name or QMessageBox.question(self, "Sablon törlése", f"Törli a(z) „{name}” sablont?",
                                           QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            self.template_store.delete(name)
        except STORE_ERRORS as exc:
            QMessageBox.warning(self, "Sablon törlési hiba", str(exc))
            return
        self.template_status.setText(f"Sablon törölve: {name}")
        self._refresh_templates()


def run_report(parent, opts, data, telephelyek, logo_path=None, attachments_dir=None):
    """Use exactly the same report model as the preview; report failures explicitly."""
    try:
        columns = opts.get("columns")
        if not columns:
            raise ValueError("Válasszon legalább egy oszlopot.")
        groups = build_report_groups(data.get("engedelyek", []), telephelyek, opts)
        if not groups:
            QMessageBox.information(parent, "Kimutatás", "A szűrésnek nincs megfelelő tétel.")
            return
        # Validate formatting before opening a destination or writing any output.
        prepare_report_groups(groups, telephelyek, columns, prune_empty=False)
        fmt = opts.get("format", "PDF")
        if fmt not in ("PDF", "XLSX"):
            raise ValueError("Nem támogatott exportformátum.")
        per_group = bool(opts.get("per_group"))
        if per_group:
            path = QFileDialog.getExistingDirectory(parent, "Mappa a csoportonkénti mentéshez")
        else:
            suffix = ".pdf" if fmt == "PDF" else ".xlsx"
            name = _safe_filename(opts.get("filename_hint") or "kimutatas")
            if not name.lower().endswith(suffix):
                name += suffix
            path, _ = QFileDialog.getSaveFileName(parent, "Kimutatás mentése", name, f"{fmt} (*{suffix})")
            if path and not path.lower().endswith(suffix):
                path += suffix
        if not path:
            return
        logo = (opts.get("logo_path") or logo_path) if opts.get("include_logo", True) else None
        export_report(path, groups, telephelyek, columns, fmt=fmt, title=opts.get("title"),
                      logo_path=logo, per_group=per_group, prune_empty=False)
    except Exception as exc:
        logger.exception("Kimutatás exportálása sikertelen")
        QMessageBox.critical(parent, "Exportálási hiba", f"A kimutatás nem készült el teljesen: {exc}")
        return
    count = sum(len(items) for items in groups.values())
    QMessageBox.information(parent, "Exportálás", f"A kimutatás elkészült ({count} tétel).")
