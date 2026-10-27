#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EngedelyDialog - engedély rögzítő / szerkesztő űrlap.
Refaktorált, típusokkal és Path használattal.

Támogatás: több felülvizsgálati bejegyzés (típus + érték + szöveg) tárolása.
UI: jobb helygazdálkodás QSplitter-rel és rugalmas méretezéssel.
Felülvizsgálat: egyszerűsítve — csak FIX dátum vagy "n nappal a lejárat előtt".
"""
from __future__ import annotations

import shutil
import mimetypes
import subprocess
import json
from uuid import uuid4
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Set

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QTextEdit,
    QComboBox, QCheckBox, QDateEdit, QSpinBox, QPushButton, QListWidget,
    QListWidgetItem, QFileDialog, QMessageBox, QGroupBox, QWidget, QTabWidget,
    QDialogButtonBox, QSizePolicy, QRadioButton, QButtonGroup, QCompleter,
    QSplitter, QInputDialog
)
from PySide6.QtCore import Qt, QDate

from config.app_config import get_ordered_categories


def qdate_to_iso(qd: QDate) -> str:
    return qd.toString("yyyy-MM-dd")


def iso_to_qdate(s: Optional[str]) -> Optional[QDate]:
    if not s:
        return None
    d = QDate.fromString(s, "yyyy-MM-dd")
    return d if d.isValid() else None


class ReviewEntryDialog(QDialog):
    """
    Dialog to add/edit a review entry.

    Supported types (simple):
     - date: exact date -> {"type": "date", "date": "YYYY-MM-DD", "note": "..."}
     - before_expiry: n days before expiry -> {"type": "before_expiry", "days": N, "note": "..."}
       (before_expiry only allowed if min_date_iso (expiry) is provided)
    The dialog aims to be compact and clear.
    """
    def __init__(
        self,
        parent=None,
        entry: Optional[Dict[str, Any]] = None,
        disallow_serials: Optional[Set[str]] = None,
        min_date_iso: Optional[str] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Felülvizsgálat bejegyzés")
        self.resize(620, 200)
        self._disallow = set(disallow_serials or set())
        self._min_date_iso = min_date_iso

        v = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)

        # Two choices: exact date OR days-before-expiry (only when expiry available).
        self.rb_date = QRadioButton("Konkrét dátum")
        self.rb_before = QRadioButton('"n" nappal a lejárat előtt')
        self.rb_group = QButtonGroup(self)
        self.rb_group.addButton(self.rb_date)
        self.rb_group.addButton(self.rb_before)

        top = QHBoxLayout()
        top.addWidget(self.rb_date)
        top.addWidget(self.rb_before)
        top.addStretch()
        v.addLayout(top)

        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.before_spin = QSpinBox()
        self.before_spin.setRange(0, 3650)
        self.before_spin.setSuffix(" nappal a lejárat előtt")
        form.addRow("Dátum:", self.date_edit)
        form.addRow("N nap:", self.before_spin)

        self.note_edit = QTextEdit()
        self.note_edit.setPlaceholderText("Megjegyzés (opcionális)")
        self.note_edit.setFixedHeight(70)
        form.addRow("Megjegyzés:", self.note_edit)

        v.addLayout(form)

        self._err_lbl = QLabel("")
        self._err_lbl.setStyleSheet("color: red;")
        v.addWidget(self._err_lbl)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self._ok_btn = btns.button(QDialogButtonBox.Ok)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        v.addWidget(btns)

        # connections
        self.rb_date.toggled.connect(self._update_enabled)
        self.rb_before.toggled.connect(self._update_enabled)
        self.date_edit.dateChanged.connect(self._validate)
        self.before_spin.valueChanged.connect(self._validate)
        self.note_edit.textChanged.connect(self._validate)

        # initialize from entry
        if entry:
            t = entry.get("type")
            if t == "date":
                d = iso_to_qdate(entry.get("date"))
                if d:
                    self.date_edit.setDate(d)
                self.rb_date.setChecked(True)
            elif t == "before_expiry":
                self.before_spin.setValue(int(entry.get("days") or 0))
                self.rb_before.setChecked(True)
            self.note_edit.setPlainText(entry.get("note") or "")
        else:
            self.rb_date.setChecked(True)

        # disable before option if no expiry provided
        if not self._min_date_iso:
            self.rb_before.setEnabled(False)
            self.rb_before.setToolTip("Lejárati dátum hiányzik; az 'n nappal' bejegyzés csak lejárathoz köthető.")
        else:
            self.rb_before.setToolTip(f"Lejárat: {self._min_date_iso}")

        self._update_enabled()
        self._validate()

    def _update_enabled(self) -> None:
        self.date_edit.setEnabled(self.rb_date.isChecked())
        self.before_spin.setEnabled(self.rb_before.isChecked() and self.rb_before.isEnabled())

    def _serialize_current(self) -> str:
        if self.rb_date.isChecked():
            return f"date|{qdate_to_iso(self.date_edit.date())}"
        return f"before|{int(self.before_spin.value())}"

    def _validate(self) -> None:
        ser = self._serialize_current()
        # if before chosen but no expiry provided, forbid
        if self.rb_before.isChecked() and not self._min_date_iso:
            self._err_lbl.setText("Nincs lejárati dátum — 'n nappal' opció nem választható.")
            self._ok_btn.setEnabled(False)
            return
        # if date chosen, and min_date provided, ensure not after expiry? or not before? keep simple: allow
        # duplicate prevention
        if ser in self._disallow:
            self._err_lbl.setText("Ugyanolyan felülvizsgálat már szerepel.")
            self._ok_btn.setEnabled(False)
            return
        self._err_lbl.setText("")
        self._ok_btn.setEnabled(True)

    def get_value(self) -> Dict[str, Any]:
        if self.rb_date.isChecked():
            return {"type": "date", "date": qdate_to_iso(self.date_edit.date()), "note": self.note_edit.toPlainText().strip()}
        return {"type": "before_expiry", "days": int(self.before_spin.value()), "note": self.note_edit.toPlainText().strip()}


class EngedelyDialog(QDialog):
    """
    EngedelyDialog: az engedély létrehozó/szerkesztő űrlap.
    Cél: egyszerű, áttekinthető felülvizsgálati kezelő, jobb elrendezés.
    """
    def __init__(
        self,
        parent=None,
        engedely: Optional[dict] = None,
        telephelyek: Optional[List[dict]] = None,
        hatosag_lista: Optional[List[str]] = None,
        attachments_base: Optional[str] = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Engedély adatlap")
        # szélesebb popup a kérésnek megfelelően
        self.resize(1100, 720)

        self.engedely = engedely or {}
        self.temp_id = str(uuid4()) if not engedely else None
        self.attachments_base = Path(attachments_base) if attachments_base else Path.cwd() / "attachment"

        # internal list of felulvizsgalat entries: list of typed dicts
        self.felulvizsgalatok: List[Dict[str, Any]] = []

        self._build_ui(telephelyek or [], hatosag_lista or [])
        self._populate_from_entity()
        self.load_attachments()
        # enable waste tab initially depending on category
        self.on_category_changed(self.kategoria.currentText())

    def _build_ui(self, telephelyek: List[dict], hatosag_lista: List[str]) -> None:
        main_v = QVBoxLayout(self)
        main_v.setSpacing(10)

        tabs = QTabWidget()
        main_v.addWidget(tabs)

        # ---------- Általános ----------
        gen_w = QWidget()
        gen_layout = QVBoxLayout(gen_w)
        gen_layout.setSpacing(8)
        splitter = QSplitter(Qt.Horizontal)

        # Left column: basic meta
        left_pane = QWidget()
        left_v = QVBoxLayout(left_pane)
        left_v.setContentsMargins(8, 8, 8, 8)
        left_v.setSpacing(8)

        left_box = QGroupBox("Alapadatok")
        form_left = QFormLayout(left_box)
        form_left.setLabelAlignment(Qt.AlignRight)
        form_left.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.telephely_combo = QComboBox()
        for t in telephelyek:
            self.telephely_combo.addItem(t.get("nev") or "", t.get("id"))

        categories = get_ordered_categories()
        self.kategoria = QComboBox()
        self.kategoria.addItems(categories)
        self.kategoria.currentTextChanged.connect(self.on_category_changed)

        self.targy = QLineEdit()
        self.targy.setPlaceholderText("Tárgy (opcionális)")
        self.targy.setMaxLength(200)
        self.targy.setMinimumWidth(240)

        self.hatarozatszam = QLineEdit()
        self.hatarozatszam.setPlaceholderText("Határozatszám (kötelező)")
        self.hatarozatszam.setMaxLength(200)
        self.hatarozatszam.setMinimumWidth(240)

        self.kiado = QLineEdit()
        self.kiado.setPlaceholderText("Kiadó hatóság")
        self.kiado.setMaxLength(200)
        if hatosag_lista:
            try:
                completer = QCompleter(sorted(set(hatosag_lista)), self.kiado)
                completer.setCaseSensitivity(Qt.CaseInsensitive)
                completer.setFilterMode(Qt.MatchContains)
                completer.setCompletionMode(QCompleter.PopupCompletion)
                self.kiado.setCompleter(completer)
            except Exception:
                pass

        form_left.addRow("Telephely*", self.telephely_combo)
        form_left.addRow("Engedélytípus*", self.kategoria)
        form_left.addRow("Tárgy", self.targy)
        form_left.addRow("Határozatszám*", self.hatarozatszam)
        form_left.addRow("Kiadó hatóság", self.kiado)
        left_v.addWidget(left_box)

        # Módosítók (compact) - kibővítve szerkesztéssel és sorrendezéssel
        modosito_box = QGroupBox("Módosító határozatok")
        modosito_v = QVBoxLayout(modosito_box)
        modosito_h = QHBoxLayout()
        self.modosito_input = QLineEdit()
        self.modosito_input.setPlaceholderText("Új módosító határozat")
        self.modosito_add_btn = QPushButton("Hozzárendel")
        modosito_h.addWidget(self.modosito_input)
        modosito_h.addWidget(self.modosito_add_btn)
        modosito_v.addLayout(modosito_h)
        self.modosito_list = QListWidget()
        self.modosito_list.setFixedHeight(110)
        modosito_v.addWidget(self.modosito_list)

        # új vezérlők: szerkesztés és sorrend módosítás
        modosito_ctl_h = QHBoxLayout()
        self.modosito_edit_btn = QPushButton("Módosítás")
        self.modosito_up_btn = QPushButton("Fel")
        self.modosito_down_btn = QPushButton("Le")
        modosito_ctl_h.addWidget(self.modosito_edit_btn)
        modosito_ctl_h.addWidget(self.modosito_up_btn)
        modosito_ctl_h.addWidget(self.modosito_down_btn)
        modosito_ctl_h.addStretch()
        modosito_v.addLayout(modosito_ctl_h)

        modosito_del_h = QHBoxLayout()
        modosito_del_h.addStretch()
        self.modosito_del_btn = QPushButton("Kiválasztott határozatszám törlése")
        modosito_del_h.addWidget(self.modosito_del_btn)
        modosito_v.addLayout(modosito_del_h)
        left_v.addWidget(modosito_box)

        # Short note
        self.megjegyzes = QTextEdit()
        self.megjegyzes.setPlaceholderText("Megjegyzés írása...")
        self.megjegyzes.setFixedHeight(80)
        left_v.addWidget(QLabel("Megjegyzés"))
        left_v.addWidget(self.megjegyzes)

        splitter.addWidget(left_pane)

        # Right column: dates & reviews
        right_pane = QWidget()
        right_v = QVBoxLayout(right_pane)
        right_v.setContentsMargins(8, 8, 8, 8)
        right_v.setSpacing(8)

        right_box = QGroupBox("Időbeli adatok")
        form_right = QFormLayout(right_box)
        form_right.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.keltezes_cb = QCheckBox("Engedély keltezése")
        self.keltezes_date = QDateEdit(QDate.currentDate())
        self.keltezes_date.setCalendarPopup(True)
        self.keltezes_date.setEnabled(False)
        self.keltezes_cb.toggled.connect(self.keltezes_date.setEnabled)
        form_right.addRow(self.keltezes_cb, self.keltezes_date)

        hataly_box = QGroupBox("Hatály")
        hb_layout = QVBoxLayout(hataly_box)
        self.rb_fixed = QRadioButton("Eddig:")
        self.rb_indef = QRadioButton("Határozatlan")
        self.rb_until_mod = QRadioButton("Változtatásig")
        self.rb_fixed.setChecked(True)
        self.hatary_date = QDateEdit(QDate.currentDate().addYears(5))
        self.hatary_date.setCalendarPopup(True)
        self.hatary_date.setEnabled(True)
        self.rb_fixed.toggled.connect(self._on_hataly_changed)
        self.rb_indef.toggled.connect(self._on_hataly_changed)
        self.rb_until_mod.toggled.connect(self._on_hataly_changed)

        hb_layout.addWidget(self.rb_fixed)
        row = QHBoxLayout()
        row.addSpacing(8)
        row.addWidget(self.hatary_date)
        row.addStretch()
        hb_layout.addLayout(row)
        hb_layout.addWidget(self.rb_indef)
        hb_layout.addWidget(self.rb_until_mod)
        form_right.addRow(hataly_box)

        # Felülvizsgálat - typed entries and compact controls (simple controls removed)
        fv_box = QGroupBox("Felülvizsgálat(ok)")
        fv_v = QVBoxLayout(fv_box)

        # typed entries list
        fv_entries_row = QHBoxLayout()
        self.fv_entries_list = QListWidget()
        self.fv_entries_list.setFixedHeight(180)
        fv_buttons_v = QVBoxLayout()
        self.btn_fv_add = QPushButton("+ Hozzáadás")
        self.btn_fv_edit = QPushButton("✎ Szerkesztés")
        self.btn_fv_del = QPushButton("- Eltávolítás")
        self.btn_fv_add.setToolTip("Új felülvizsgálati bejegyzés hozzáadása")
        self.btn_fv_edit.setToolTip("Kiválasztott bejegyzés szerkesztése")
        self.btn_fv_del.setToolTip("Kiválasztott bejegyzés eltávolítása")
        fv_buttons_v.addWidget(self.btn_fv_add)
        fv_buttons_v.addWidget(self.btn_fv_edit)
        fv_buttons_v.addWidget(self.btn_fv_del)
        fv_buttons_v.addStretch()
        fv_entries_row.addWidget(self.fv_entries_list, 1)
        fv_entries_row.addLayout(fv_buttons_v)
        fv_v.addLayout(fv_entries_row)

    # --- környező rész a fájlból ---
        self.next_review_lbl = QLabel("")
        # korábban: self.next_review_lbl.setStyleSheet("font-weight: bold;")
        # új: legyen vastag és piros a figyelem felkeltéséhez
        self.next_review_lbl.setStyleSheet("font-weight: bold; color: red;")
        fv_v.addWidget(self.next_review_lbl)
        # --- vége ---
        fv_v.addWidget(self.next_review_lbl)

        form_right.addRow(fv_box)
        right_v.addWidget(right_box)
        right_v.addStretch()

        splitter.addWidget(right_pane)

        # jobb oldali panel nagyobb súlyt kap (hogy szélesebb legyen a jobb rész)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([600, 480])

        gen_layout.addWidget(splitter)
        tabs.addTab(gen_w, "Általános")

        # ---------- Hulladék (simple rows) ----------
        waste_w = QWidget()
        waste_layout = QFormLayout(waste_w)

        def create_waste_row():
            inp = QLineEdit()
            inp.setPlaceholderText("Mennyiség")
            inp.setMaxLength(50)
            combo = QComboBox()
            combo.addItems(["t", "kg"])
            combo.setFixedWidth(60)
            h = QHBoxLayout()
            w = QWidget()
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(inp)
            h.addWidget(combo)
            w.setLayout(h)
            return inp, combo, w

        self.w_nv_elokez_inp, self.w_nv_elokez_unit, w1 = create_waste_row()
        self.w_v_elokez_inp, self.w_v_elokez_unit, w2 = create_waste_row()
        self.w_nv_gyujt_inp, self.w_nv_gyujt_unit, w3 = create_waste_row()
        self.w_v_gyujt_inp, self.w_v_gyujt_unit, w4 = create_waste_row()

        waste_layout.addRow("Nem veszélyes előkezelés", w1)
        waste_layout.addRow("Veszélyes előkezelés", w2)
        waste_layout.addRow("Nem veszélyes gyűjtés", w3)
        waste_layout.addRow("Veszélyes gyűjtés", w4)
        tabs.addTab(waste_w, "Hulladék")
        tabs.setTabEnabled(tabs.indexOf(waste_w), False)

        # ---------- Csatolmányok ----------
        att_w = QWidget()
        att_layout = QHBoxLayout(att_w)
        left_att_col = QVBoxLayout()
        left_att_col.addWidget(QLabel("Csatolt fájlok:"))
        self.attach_list = QListWidget()
        left_att_col.addWidget(self.attach_list)
        att_layout.addLayout(left_att_col, 1)

        right_att_col = QVBoxLayout()
        self.attach_add = QPushButton("+ Fájl hozzáadása")
        self.attach_del = QPushButton("- Fájl törlése")
        self.attach_view = QPushButton("👁 Megtekintés")
        right_att_col.addWidget(self.attach_add)
        right_att_col.addWidget(self.attach_del)
        right_att_col.addWidget(self.attach_view)
        right_att_col.addStretch()
        att_layout.addLayout(right_att_col)
        self.attach_add.clicked.connect(self.add_attachment)
        self.attach_del.clicked.connect(self.remove_attachment)
        self.attach_view.clicked.connect(self.view_attachment)
        tabs.addTab(att_w, "Csatolmányok")

        # buttons (Save/Cancel)
        btns = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save_btn = btns.button(QDialogButtonBox.Save)
        save_btn.setText("Mentés")
        cancel_btn = btns.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("Mégse")
        btns.accepted.connect(self._on_accept)
        btns.rejected.connect(self.reject)
        main_v.addWidget(btns)

        # wire up modosito + fv buttons
        self.modosito_add_btn.clicked.connect(self._on_modosito_add)
        self.modosito_del_btn.clicked.connect(self._on_modosito_del)
        self.modosito_input.returnPressed.connect(self._on_modosito_add)

        # új módosító vezérlők
        self.modosito_edit_btn.clicked.connect(self._on_modosito_edit)
        self.modosito_up_btn.clicked.connect(self._on_modosito_move_up)
        self.modosito_down_btn.clicked.connect(self._on_modosito_move_down)
        self.modosito_list.itemDoubleClicked.connect(self._on_modosito_edit)

        self.btn_fv_add.clicked.connect(self.add_review_entry)
        self.btn_fv_edit.clicked.connect(self.edit_review_entry)
        self.btn_fv_del.clicked.connect(self.remove_review_entry)

        # initial states
        self._on_hataly_changed()
        self._update_fv_entries_list()

    # ---------------- modosito handlers ----------------
    def _on_modosito_add(self) -> None:
        txt = self.modosito_input.text().strip()
        if not txt:
            return
        existing = [self.modosito_list.item(i).text() for i in range(self.modosito_list.count())]
        if txt.lower() in (e.lower() for e in existing):
            QMessageBox.information(self, "Duplikáció", "Ez a módosító már szerepel.")
            return
        item = QListWidgetItem(txt)
        item.setToolTip(txt)
        self.modosito_list.addItem(item)
        self.modosito_input.clear()

    def _on_modosito_edit(self, item: Optional[QListWidgetItem] = None) -> None:
        # lehet meghívni gombból (nincs param) vagy dupla-kattból (item param)
        sel_item = item or self.modosito_list.currentItem()
        if not sel_item:
            return
        old = sel_item.text()
        new_text, ok = QInputDialog.getText(self, "Módosító szerkesztése", "Határozatszám:", text=old)
        if not ok:
            return
        new_text = new_text.strip()
        if not new_text:
            QMessageBox.information(self, "Érvénytelen", "A megadott szöveg üres.")
            return
        existing = [self.modosito_list.item(i).text() for i in range(self.modosito_list.count())]
        # engedjük a saját érték megtartását; ellenőrizzük az egyéb duplikátumokat
        lower_existing = [e.lower() for e in existing]
        if new_text.lower() in (e for idx, e in enumerate(lower_existing) if existing[idx] != old):
            QMessageBox.information(self, "Duplikáció", "Ez a módosító már szerepel.")
            return
        sel_item.setText(new_text)
        sel_item.setToolTip(new_text)

    def _on_modosito_move_up(self) -> None:
        idx = self.modosito_list.currentRow()
        if idx <= 0:
            return
        item = self.modosito_list.takeItem(idx)
        self.modosito_list.insertItem(idx - 1, item)
        self.modosito_list.setCurrentRow(idx - 1)

    def _on_modosito_move_down(self) -> None:
        idx = self.modosito_list.currentRow()
        if idx < 0 or idx >= self.modosito_list.count() - 1:
            return
        item = self.modosito_list.takeItem(idx)
        self.modosito_list.insertItem(idx + 1, item)
        self.modosito_list.setCurrentRow(idx + 1)

    def _on_modosito_del(self) -> None:
        sel = self.modosito_list.currentItem()
        if not sel:
            return
        if QMessageBox.question(self, "Törlés", f"Tényleg törli a kiválasztott módosítót?\n{sel.text()}") != QMessageBox.Yes:
            return
        self.modosito_list.takeItem(self.modosito_list.currentRow())

    # ---------------- hataly / fv logic ----------------
    def _on_hataly_changed(self, _=None) -> None:
        # hatály/változás állapot változásakor frissítjük a felülvizsgálati megjelenítést
        # (a részletes felülvizsgálati módválasztás a Hozzáadás párbeszédben történik)
        self._update_fv_entries_list()

    # ---------------- populate ----------------
    def _populate_from_entity(self) -> None:
        e = self.engedely
        if not e:
            return
        try:
            idx = self.telephely_combo.findData(e.get("telephely_id"))
            if idx >= 0:
                self.telephely_combo.setCurrentIndex(idx)
            self.targy.setText(e.get("engedely_nev") or "")
            self.targy.setFixedHeight(50)
            if e.get("kategoria"):
                self.kategoria.setCurrentText(e.get("kategoria"))
            self.kiado.setText(e.get("kiado_hatosag") or "")
            self.hatarozatszam.setText(e.get("hatarozatszam") or "")

            modos = []
            raw_mod = e.get("modosito_hat")
            if raw_mod:
                modos = [p.strip() for p in str(raw_mod).split(";") if p.strip()]
            new_mods = e.get("modositok")
            if new_mods and isinstance(new_mods, (list, tuple)):
                modos = [str(x) for x in new_mods if x]
            for m in modos:
                item = QListWidgetItem(m)
                item.setToolTip(m)
                self.modosito_list.addItem(item)

            d = iso_to_qdate(e.get("letrejott"))
            if d:
                self.keltezes_cb.setChecked(True)
                self.keltezes_date.setDate(d)

            hataly = e.get("hataly_tipus") or ("indefinite" if e.get("hatarozatlan") else "fixed")
            if hataly == "fixed":
                self.rb_fixed.setChecked(True)
                d_lej = iso_to_qdate(e.get("lejarat"))
                if d_lej:
                    self.hatary_date.setDate(d_lej)
            elif hataly == "indefinite":
                self.rb_indef.setChecked(True)
            else:
                self.rb_until_mod.setChecked(True)

            # load typed felulvizsgalatok (support only 'date' and 'before_expiry' shapes)
            self.felulvizsgalatok = []
            fv_list_raw = e.get("felulvizsgalatok")
            if fv_list_raw:
                try:
                    if isinstance(fv_list_raw, str):
                        parsed = json.loads(fv_list_raw)
                    else:
                        parsed = fv_list_raw
                    if isinstance(parsed, list):
                        for it in parsed:
                            if isinstance(it, dict) and it.get("type") in ("date", "before_expiry"):
                                self.felulvizsgalatok.append(it)
                except Exception:
                    self.felulvizsgalatok = []

            # backwards: synthesize from old fields if necessary
            if not self.felulvizsgalatok:
                fv_date = iso_to_qdate(e.get("felulvizsgalat_date"))
                fv_nap = int(e.get("felulvizsgalat_nap") or 0)
                if fv_date:
                    self.felulvizsgalatok.append({"type": "date", "date": qdate_to_iso(fv_date), "note": ""})
                elif fv_nap > 0:
                    self.felulvizsgalatok.append({"type": "before_expiry", "days": fv_nap, "note": ""})

            self.megjegyzes.setPlainText(e.get("megjegyzes") or "")

            def load_waste(full_str: Optional[str], inp_widget: QLineEdit, unit_widget: QComboBox):
                if not full_str:
                    return
                parts = full_str.split(" ")
                if len(parts) >= 2 and parts[-1] in ["t", "kg"]:
                    unit_widget.setCurrentText(parts[-1])
                    inp_widget.setText(" ".join(parts[:-1]))
                else:
                    inp_widget.setText(full_str)
                    unit_widget.setCurrentText("t")

            load_waste(e.get("hulladek_nv_elokez"), self.w_nv_elokez_inp, self.w_nv_elokez_unit)
            load_waste(e.get("hulladek_v_elokez"), self.w_v_elokez_inp, self.w_v_elokez_unit)
            load_waste(e.get("hulladek_nv_gyujt"), self.w_nv_gyujt_inp, self.w_nv_gyujt_unit)
            load_waste(e.get("hulladek_v_gyujt"), self.w_v_gyujt_inp, self.w_v_gyujt_unit)
        except Exception:
            pass
        finally:
            self._on_hataly_changed()
            self._sanitize_review_entries()
            self._update_fv_entries_list()

    # ---------------- review entries management ----------------
    def _existing_review_serials(self) -> Set[str]:
        s: Set[str] = set()
        for it in self.felulvizsgalatok:
            t = it.get("type")
            if t == "date":
                s.add(f"date|{it.get('date')}")
            elif t == "before_expiry":
                s.add(f"before|{int(it.get('days'))}")
        return s

    def _sanitize_review_entries(self) -> None:
        """Sort and dedupe review entries by serialized form (date before before_expiry)."""
        cleaned: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        for it in self.felulvizsgalatok:
            t = it.get("type")
            if t == "date" and it.get("date"):
                ser = f"date|{it['date']}"
            elif t == "before_expiry" and ("days" in it):
                ser = f"before|{int(it['days'])}"
            else:
                continue
            if ser in seen:
                continue
            seen.add(ser)
            cleaned.append(it)
        # sort by date then by days
        def sort_key(it):
            t = it.get("type")
            if t == "date":
                return (0, it.get("date"))
            return (1, int(it.get("days")))
        self.felulvizsgalatok = sorted(cleaned, key=sort_key)

    def _fv_display_text(self, it: Dict[str, Any]) -> str:
        t = it.get("type")
        note = it.get("note") or ""
        if t == "date":
            txt = it.get("date", "")
        else:
            txt = f"{int(it.get('days'))} nappal a lejárat előtt"
        return f"{txt}" + (f" — {note}" if note else "")

    def _update_fv_entries_list(self) -> None:
        self.fv_entries_list.clear()
        for it in self.felulvizsgalatok:
            disp = self._fv_display_text(it)
            item = QListWidgetItem(disp if len(disp) <= 150 else disp[:147] + "...")
            item.setToolTip(disp)
            item.setData(Qt.UserRole, it)
            self.fv_entries_list.addItem(item)
        self._update_next_review_label()

    def add_review_entry(self) -> None:
        expiry_iso = None
        if self.rb_fixed.isChecked():
            expiry_iso = qdate_to_iso(self.hatary_date.date()) if self.hatary_date else None
        disallow = self._existing_review_serials()
        dlg = ReviewEntryDialog(self, entry=None, disallow_serials=disallow, min_date_iso=expiry_iso)
        if dlg.exec() != QDialog.Accepted:
            return
        val = dlg.get_value()
        if val and val.get("type"):
            self.felulvizsgalatok.append(val)
            self._sanitize_review_entries()
            self._update_fv_entries_list()

    def edit_review_entry(self) -> None:
        sel = self.fv_entries_list.currentItem()
        if not sel:
            return
        data = sel.data(Qt.UserRole) or {}
        idx = self.fv_entries_list.currentRow()
        expiry_iso = None
        if self.rb_fixed.isChecked():
            expiry_iso = qdate_to_iso(self.hatary_date.date()) if self.hatary_date else None
        disallow = self._existing_review_serials()
        # allow the currently edited serial
        if data:
            t = data.get("type")
            if t == "date":
                disallow.discard(f"date|{data.get('date')}")
            elif t == "before_expiry":
                disallow.discard(f"before|{int(data.get('days'))}")
        dlg = ReviewEntryDialog(self, entry=data, disallow_serials=disallow, min_date_iso=expiry_iso)
        if dlg.exec() != QDialog.Accepted:
            return
        val = dlg.get_value()
        if val:
            try:
                self.felulvizsgalatok[idx] = val
            except Exception:
                self.felulvizsgalatok.append(val)
            self._sanitize_review_entries()
            self._update_fv_entries_list()

    def remove_review_entry(self) -> None:
        sel = self.fv_entries_list.currentItem()
        if not sel:
            return
        if QMessageBox.question(self, "Törlés", "Tényleg törli a kiválasztott felülvizsgálati bejegyzést?") != QMessageBox.Yes:
            return
        idx = self.fv_entries_list.currentRow()
        try:
            self.felulvizsgalatok.pop(idx)
        except Exception:
            pass
        self._update_fv_entries_list()

    def _update_next_review_label(self) -> None:
        if not self.felulvizsgalatok:
            self.next_review_lbl.setText("Következő felülvizsgálat: nincs bejegyzés")
            return
        today = date.today()
        today_iso = today.isoformat()
        candidates: List[tuple] = []
        for it in self.felulvizsgalatok:
            if it.get("type") == "date":
                d = it.get("date")
                if d and d >= today_iso:
                    candidates.append((d, it))
            elif it.get("type") == "before_expiry":
                # need expiry to compute actual date
                if not self.rb_fixed.isChecked():
                    continue
                try:
                    expiry = iso_to_qdate(qdate_to_iso(self.hatary_date.date()))
                    if not expiry:
                        continue
                    expiry_dt = date(expiry.year(), expiry.month(), expiry.day())
                    days = int(it.get("days") or 0)
                    cand_dt = expiry_dt - timedelta(days=days)
                    cand_iso = cand_dt.isoformat()
                    if cand_iso >= today_iso:
                        candidates.append((cand_iso, it))
                except Exception:
                    continue
        if candidates:
            nxt = min(candidates, key=lambda x: x[0])
            it = nxt[1]
            label = it.get("note") or ""
            self.next_review_lbl.setText(f"Következő felülvizsgálat: {nxt[0]} — {label}")
        else:
            self.next_review_lbl.setText("Következő felülvizsgálat: nincs jövőbeli bejegyzés (csak fix dátumok számíthatók)")

    # ---------------- attachments (unchanged) ----------------
    def get_attach_dir(self) -> Path:
        if self.engedely and self.engedely.get("id"):
            return self.attachments_base / self.engedely["id"]
        else:
            return self.attachments_base / f"temp_{self.temp_id}"

    def load_attachments(self) -> None:
        self.attach_list.clear()
        adir = self.get_attach_dir()
        if adir.exists() and adir.is_dir():
            for fname in sorted([p.name for p in adir.iterdir() if p.is_file()]):
                self.attach_list.addItem(fname)

    def add_attachment(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Fájl(ok) kiválasztása", str(Path.home()), "Minden fájl (*.*)")
        if not files:
            return
        adir = self.get_attach_dir()
        adir.mkdir(parents=True, exist_ok=True)
        for f in files:
            src = Path(f)
            dest = adir / src.name
            try:
                if dest.exists():
                    stem, ext = dest.stem, dest.suffix
                    dest = adir / f"{stem}_{int(datetime.now().timestamp())}{ext}"
                shutil.copy2(str(src), str(dest))
            except Exception as e:
                QMessageBox.warning(self, "Hiba", f"Nem sikerült másolni: {f}\n{e}")
        self.load_attachments()

    def remove_attachment(self) -> None:
        sel = self.attach_list.currentItem()
        if not sel:
            return
        fname = sel.text()
        if QMessageBox.question(self, "Törlés", f"Tényleg törli a kiválasztott fájlt?\n{fname}") != QMessageBox.Yes:
            return
        fpath = self.get_attach_dir() / fname
        try:
            if fpath.exists():
                fpath.unlink()
            adir = self.get_attach_dir()
            if adir.exists() and not any(adir.iterdir()):
                try:
                    adir.rmdir()
                except Exception:
                    pass
            self.load_attachments()
        except Exception as e:
            QMessageBox.warning(self, "Hiba", f"Nem sikerült törölni: {e}")

    def view_attachment(self) -> None:
        sel = self.attach_list.currentItem()
        if not sel:
            return
        fpath = self.get_attach_dir() / sel.text()
        if not fpath.exists():
            QMessageBox.warning(self, "Hiba", "Fájl nem található.")
            self.load_attachments()
            return
        mime, _ = mimetypes.guess_type(str(fpath))
        if mime and mime.startswith("image"):
            try:
                dlg = QDialog(self)
                dlg.setWindowTitle(fpath.name)
                dlg.resize(900, 700)
                l = QVBoxLayout(dlg)
                from PySide6.QtGui import QPixmap

                pix = QPixmap(str(fpath))
                if pix.isNull():
                    l.addWidget(QLabel("Nem sikerült a kép betöltése."))
                else:
                    lbl_img = QLabel()
                    lbl_img.setAlignment(Qt.AlignCenter)
                    lbl_img.setPixmap(pix.scaled(dlg.size() * 0.9, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                    l.addWidget(lbl_img)
                dlg.exec()
            except Exception:
                QMessageBox.warning(self, "Hiba", "Nem sikerült megjeleníteni a képet.")
        else:
            try:
                import sys
                if sys.platform.startswith("win"):
                    import os
                    os.startfile(str(fpath))
                elif sys.platform.startswith("darwin"):
                    subprocess.Popen(["open", str(fpath)])
                else:
                    subprocess.Popen(["xdg-open", str(fpath)])
            except Exception as e:
                QMessageBox.warning(self, "Hiba", f"Nem sikerült megnyitni: {e}")

    # ---------------- collect data ----------------
    def get_data(self) -> dict:
        data = {
            "telephely_id": self.telephely_combo.currentData(),
            "engedely_nev": self.targy.text().strip() or None,
            "kategoria": self.kategoria.currentText(),
            "kiado_hatosag": self.kiado.text().strip() or None,
            "hatarozatszam": self.hatarozatszam.text().strip(),
            "modosito_hat": None,
            "modositok": None,
            "letrejott": qdate_to_iso(self.keltezes_date.date()) if self.keltezes_cb.isChecked() else None,
            "megjegyzes": self.megjegyzes.toPlainText().strip() or None,
        }

        mod_list = [self.modosito_list.item(i).text() for i in range(self.modosito_list.count())]
        if mod_list:
            data["modositok"] = mod_list
            data["modosito_hat"] = ";".join(mod_list)

        if self.rb_fixed.isChecked():
            data["hataly_tipus"] = "fixed"
            data["lejarat"] = qdate_to_iso(self.hatary_date.date())
            data["hatarozatlan"] = 0
        elif self.rb_indef.isChecked():
            data["hataly_tipus"] = "indefinite"
            data["lejarat"] = None
            data["hatarozatlan"] = 1
        else:
            data["hataly_tipus"] = "until_modified"
            data["lejarat"] = None
            data["hatarozatlan"] = 0

        # alapértelmezett egyszerű mezők (a tényleges értékeket a typed felülvizsgálatokból szintetizáljuk)
        data["felulvizsgalat_nap"] = 0
        data["felulvizsgalat_date"] = None

        # typed felulvizsgalatok
        self._sanitize_review_entries()
        if self.felulvizsgalatok:
            data["felulvizsgalatok"] = self.felulvizsgalatok.copy()
            date_entries = [it for it in self.felulvizsgalatok if it.get("type") == "date"]
            before_entries = [it for it in self.felulvizsgalatok if it.get("type") == "before_expiry"]
            if len(date_entries) == 1 and not data.get("felulvizsgalat_date"):
                data["felulvizsgalat_date"] = date_entries[0].get("date")
            if len(before_entries) == 1 and not data.get("felulvizsgalat_nap"):
                data["felulvizsgalat_nap"] = int(before_entries[0].get("days") or 0)

        def save_waste(inp: QLineEdit, unit: QComboBox) -> Optional[str]:
            txt = inp.text().strip()
            return f"{txt} {unit.currentText()}" if txt else None

        if data["kategoria"] == "Hulladékgazdálkodási engedély":
            data.update({
                "hulladek_nv_elokez": save_waste(self.w_nv_elokez_inp, self.w_nv_elokez_unit),
                "hulladek_v_elokez": save_waste(self.w_v_elokez_inp, self.w_v_elokez_unit),
                "hulladek_nv_gyujt": save_waste(self.w_nv_gyujt_inp, self.w_nv_gyujt_unit),
                "hulladek_v_gyujt": save_waste(self.w_v_gyujt_inp, self.w_v_gyujt_unit),
            })
        else:
            data.update({
                "hulladek_nv_elokez": None, "hulladek_v_elokez": None,
                "hulladek_nv_gyujt": None, "hulladek_v_gyujt": None
            })

        data["attachments_temp"] = None
        if not self.engedely:
            tmp = self.get_attach_dir()
            if tmp.exists() and any(tmp.iterdir()):
                data["attachments_temp"] = str(tmp)
        return data

    # ---------------- validation / accept ----------------
    def _on_accept(self) -> None:
        if not self.telephely_combo.currentData():
            QMessageBox.warning(self, "Hiányzó adat", "Válasszon telephelyet a legördülőből.")
            return
        if not self.hatarozatszam.text().strip():
            QMessageBox.warning(self, "Hiányzó adat", "A határozatszám megadása kötelező.")
            return
        self._sanitize_review_entries()
        self.accept()

    # ---------------- category/tab helper ----------------
    def on_category_changed(self, text: str) -> None:
        tw: QTabWidget = self.findChild(QTabWidget)
        if not tw:
            return
        for i in range(tw.count()):
            if tw.tabText(i) == "Hulladék":
                tw.setTabEnabled(i, text == "Hulladékgazdálkodási engedély")
                break