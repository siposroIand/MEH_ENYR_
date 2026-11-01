"""Hungarian fixed-layout OCR editor with page-relative mouse selections."""
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QStandardPaths, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMessageBox, QPushButton, QSpinBox, QSplitter,
    QVBoxLayout, QWidget,
)

from core.document_ocr import SUPPORTED_EXTENSIONS
from core.ocr_templates import (
    FIELD_LABELS, OcrRegion, OcrTemplate, TemplateStore,
    document_page_count, render_document_page,
)


class RegionPreview(QGraphicsView):
    region_selected = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing)
        self.setBackgroundBrush(QColor("#555b63"))
        self.setMinimumSize(300, 300)
        self.setCursor(Qt.CrossCursor)
        self._page_rect = QRectF()
        self._start = None
        self._rubber = None
        self._overlays = []
        self._fit = True

    def set_image(self, image):
        rgb = image.convert("RGB")
        try:
            width, height = rgb.size
            qimage = QImage(
                rgb.tobytes(), width, height, width * 3, QImage.Format_RGB888,
            ).copy()
        finally:
            rgb.close()
        self.scene().clear()
        self._overlays = []
        self._rubber = None
        self._start = None
        self.scene().addPixmap(QPixmap.fromImage(qimage))
        self._page_rect = QRectF(0, 0, width, height)
        self.setSceneRect(self._page_rect)
        self.fit_page()

    def clear_page(self):
        self.scene().clear()
        self._page_rect = QRectF()
        self._overlays = []
        self._rubber = None
        self._start = None

    def fit_page(self):
        self._fit = True
        if not self._page_rect.isEmpty():
            self.fitInView(self._page_rect, Qt.KeepAspectRatio)

    def zoom(self, factor):
        if self._page_rect.isEmpty():
            return
        scale = self.transform().m11() * factor
        if 0.03 <= scale <= 8:
            self._fit = False
            self.scale(factor, factor)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._fit:
            self.fit_page()

    def show_regions(self, regions: list[OcrRegion], active_field: str):
        for item in self._overlays:
            self.scene().removeItem(item)
        self._overlays = []
        if self._page_rect.isEmpty():
            return
        for region in regions:
            x0, y0, x1, y1 = region.rect
            width, height = self._page_rect.width(), self._page_rect.height()
            rect = QRectF(x0 * width, y0 * height, (x1 - x0) * width, (y1 - y0) * height)
            color = QColor("#0075db" if region.field == "subject" else "#b35600")
            pen = QPen(color, 3 if region.field == active_field else 2)
            pen.setCosmetic(True)
            fill = QColor(color)
            fill.setAlpha(35)
            item = self.scene().addRect(rect, pen, fill)
            label = self.scene().addSimpleText(FIELD_LABELS[region.field])
            label.setBrush(color)
            label.setPos(rect.topLeft())
            label.setFlag(label.GraphicsItemFlag.ItemIgnoresTransformations)
            self._overlays.extend((item, label))

    def _clamp(self, point: QPointF) -> QPointF:
        return QPointF(
            min(max(point.x(), 0), self._page_rect.width()),
            min(max(point.y(), 0), self._page_rect.height()),
        )

    def mousePressEvent(self, event):
        point = self.mapToScene(event.position().toPoint())
        if event.button() == Qt.LeftButton and self._page_rect.contains(point):
            self._start = point
            pen = QPen(QColor("#d12b39"), 2, Qt.DashLine)
            pen.setCosmetic(True)
            self._rubber = self.scene().addRect(QRectF(point, point), pen)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._start is not None:
            point = self._clamp(self.mapToScene(event.position().toPoint()))
            self._rubber.setRect(QRectF(self._start, point).normalized())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._start is not None:
            point = self._clamp(self.mapToScene(event.position().toPoint()))
            rect = QRectF(self._start, point).normalized()
            self.scene().removeItem(self._rubber)
            self._rubber = None
            self._start = None
            if rect.width() >= 3 and rect.height() >= 3:
                width, height = self._page_rect.width(), self._page_rect.height()
                self.region_selected.emit((
                    rect.left() / width, rect.top() / height,
                    rect.right() / width, rect.bottom() / height,
                ))
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class OcrTemplateEditor(QDialog):
    def __init__(self, store: TemplateStore, template: OcrTemplate | None = None,
                 document_paths=(), parent=None, *, authority: str = ""):
        super().__init__(parent)
        self.store = store
        self.template = template
        self.saved_template: OcrTemplate | None = None
        self.regions = list(template.regions) if template else []
        self.document_path: Path | None = None
        self._dirty = False
        self.setWindowTitle("Hatósági OCR-sablon szerkesztése")
        self.resize(1080, 720)
        layout = QVBoxLayout(self)
        instructions = QLabel(
            "1. Adja meg a hatóságot és a sablon nevét.  2. Nyisson meg egy mintadokumentumot.\n"
            "3. Válasszon oldalt és mezőt, majd egérhúzással jelölje ki annak szövegét.\n"
            "A sablon rögzített elrendezéshez való: más oldalszám vagy elrendezés esetén "
            "ellenőrizze a kijelölést. A fel nem vett mezők üresen maradnak."
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        form = QFormLayout()
        self.name_edit = QLineEdit(template.name if template else "")
        self.authority_edit = QLineEdit(template.authority if template else authority)
        self.name_edit.setMaxLength(150)
        self.authority_edit.setMaxLength(150)
        self.name_edit.textEdited.connect(self._mark_dirty)
        self.authority_edit.textEdited.connect(self._mark_dirty)
        form.addRow("Hatóság:", self.authority_edit)
        form.addRow("Sablon neve:", self.name_edit)
        layout.addLayout(form)
        source_row = QHBoxLayout()
        self.source_combo = QComboBox()
        self.source_combo.setMinimumContentsLength(20)
        self.source_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.source_combo.addItem("Válasszon mintadokumentumot…", None)
        for path in document_paths:
            path = Path(path)
            if path.suffix.lower() in SUPPORTED_EXTENSIONS:
                self.source_combo.addItem(path.name, path)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        source_row.addWidget(self.source_combo, 1)
        browse = QPushButton("Minta megnyitása…")
        browse.clicked.connect(self._browse)
        source_row.addWidget(browse)
        layout.addLayout(source_row)
        splitter = QSplitter(Qt.Horizontal)
        self.preview = RegionPreview()
        self.preview.region_selected.connect(self._region_selected)
        splitter.addWidget(self.preview)
        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        controls_form = QFormLayout()
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setEnabled(False)
        self.page_spin.valueChanged.connect(self._load_page)
        controls_form.addRow("Oldal:", self.page_spin)
        self.field_combo = QComboBox()
        for key, label in FIELD_LABELS.items():
            self.field_combo.addItem(label, key)
        self.field_combo.currentIndexChanged.connect(self._refresh_overlay)
        controls_form.addRow("Kijelölendő mező:", self.field_combo)
        controls_layout.addLayout(controls_form)
        controls_layout.addWidget(QLabel("Kijelölt területek (oldalanként):"))
        self.region_list = QListWidget()
        self.region_list.currentRowChanged.connect(self._select_region)
        controls_layout.addWidget(self.region_list, 1)
        remove = QPushButton("Kijelölt terület törlése")
        remove.clicked.connect(self._remove_region)
        controls_layout.addWidget(remove)
        hint = QLabel(
            "Ugyanazon mező újrarajzolása ezen az oldalon lecseréli a területét. "
            "Több oldalon ismétlődő, eltérő értékeknél a program nem választ önkényesen.\n\n"
            "Tárgy: csak a tárgy szövegét jelölje ki.\n"
            "Hatály: csak a lejáratot / érvényességi időszakot jelölje ki, ne a keltezést."
        )
        hint.setWordWrap(True)
        controls_layout.addWidget(hint)
        controls.setMaximumWidth(320)
        splitter.addWidget(controls)
        splitter.setStretchFactor(0, 1)
        layout.addWidget(splitter, 1)
        zoom_row = QHBoxLayout()
        for label, callback in (
            ("Nagyítás +", lambda: self.preview.zoom(1.3)),
            ("Kicsinyítés −", lambda: self.preview.zoom(1 / 1.3)),
            ("Teljes oldal", self.preview.fit_page),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            zoom_row.addWidget(button)
        zoom_row.addStretch()
        layout.addLayout(zoom_row)
        self.status = QLabel("A minta és a felismert szöveg nem kerül a sablonba; minden feldolgozás helyi.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox()
        save = buttons.addButton("Sablon mentése", QDialogButtonBox.AcceptRole)
        save.clicked.connect(self._save)
        if template:
            copy = buttons.addButton("Mentés új sablonként", QDialogButtonBox.ActionRole)
            copy.clicked.connect(lambda: self._save(as_new=True))
            delete = buttons.addButton("Sablon törlése", QDialogButtonBox.DestructiveRole)
            delete.clicked.connect(self._delete)
        close = buttons.addButton("Mégse", QDialogButtonBox.RejectRole)
        close.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_regions()
        if self.source_combo.count() > 1:
            self.source_combo.setCurrentIndex(1)

    def _mark_dirty(self, *_):
        self._dirty = True

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Mintadokumentum megnyitása", "",
            "Dokumentumok (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp)",
        )
        if path:
            self.source_combo.addItem(Path(path).name, Path(path))
            self.source_combo.setCurrentIndex(self.source_combo.count() - 1)

    def _source_changed(self):
        path = self.source_combo.currentData()
        self.document_path = None
        self.preview.clear_page()
        self.page_spin.setEnabled(False)
        if path is None:
            return
        try:
            count = document_page_count(path)
            if count < 1:
                raise ValueError("A dokumentum nem tartalmaz oldalt.")
            self.document_path = path
            self.page_spin.blockSignals(True)
            self.page_spin.setRange(1, count)
            self.page_spin.setValue(1)
            self.page_spin.blockSignals(False)
            self.page_spin.setEnabled(True)
            self._load_page()
        except Exception as exc:
            self.status.setText(f"A mintadokumentum nem nyitható meg: {exc}")

    def _load_page(self):
        if self.document_path is None:
            return
        try:
            with render_document_page(self.document_path, self.page_spin.value()) as image:
                self.preview.set_image(image)
            self.status.setText(
                f"{self.document_path.name} — {self.page_spin.value()}. oldal. "
                "A kijelölés az oldal méretéhez arányosítva mentődik."
            )
            self._refresh_overlay()
        except Exception as exc:
            self.preview.clear_page()
            self.status.setText(f"Az oldal előnézete nem készíthető el: {exc}")

    def _region_selected(self, rect):
        try:
            region = OcrRegion(self.field_combo.currentData(), self.page_spin.value(), rect)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self.regions = [
            r for r in self.regions if (r.field, r.page) != (region.field, region.page)
        ] + [region]
        self._dirty = True
        self._refresh_regions()

    def _refresh_regions(self):
        self.regions.sort(key=lambda r: (r.page, r.field))
        self.region_list.blockSignals(True)
        self.region_list.clear()
        for region in self.regions:
            x0, y0, x1, y1 = region.rect
            self.region_list.addItem(
                f"{region.page}. oldal – {FIELD_LABELS[region.field]}\n"
                f"({x0:.0%}, {y0:.0%}) – ({x1:.0%}, {y1:.0%})"
            )
        self.region_list.blockSignals(False)
        self._refresh_overlay()

    def _refresh_overlay(self):
        self.preview.show_regions(
            [r for r in self.regions if r.page == self.page_spin.value()],
            self.field_combo.currentData(),
        )

    def _select_region(self, index):
        if not 0 <= index < len(self.regions):
            return
        region = self.regions[index]
        self.field_combo.setCurrentIndex(self.field_combo.findData(region.field))
        if self.document_path and region.page <= self.page_spin.maximum():
            self.page_spin.setValue(region.page)
        elif self.document_path:
            self.status.setText(f"A mintadokumentumnak nincs {region.page}. oldala.")
        self._refresh_overlay()

    def _remove_region(self):
        index = self.region_list.currentRow()
        if index >= 0:
            del self.regions[index]
            self._dirty = True
            self._refresh_regions()

    def _save(self, checked=False, *, as_new=False):
        try:
            if self.template and not as_new:
                template = OcrTemplate(
                    self.template.id, self.name_edit.text().strip(),
                    self.authority_edit.text().strip(), tuple(self.regions),
                )
            else:
                template = OcrTemplate.create(
                    self.name_edit.text(), self.authority_edit.text(), self.regions,
                )
            self.store.save(template)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "A sablon nem menthető", str(exc))
            return
        self.saved_template = template
        self._dirty = False
        self.accept()

    def _delete(self):
        if QMessageBox.question(
            self, "Sablon törlése", "Biztosan törli ezt a sablont?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        try:
            self.store.delete(self.template)
        except OSError as exc:
            QMessageBox.warning(self, "A sablon nem törölhető", str(exc))
            return
        self._dirty = False
        self.accept()

    def reject(self):
        if self._dirty and QMessageBox.question(
            self, "Nem mentett módosítások", "Elveti a sablon nem mentett módosításait?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        super().reject()


class OcrTemplateSelector(QWidget):
    """Embeddable selector. None retains existing automatic, labelled-text OCR."""

    def __init__(self, parent=None, *, store: TemplateStore | None = None,
                 authority: str = ""):
        super().__init__(parent)
        self.store = store or TemplateStore(
            Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)) / "ocr_templates",
        )
        self.document_paths: list[Path] = []
        self._authority = authority.strip()
        self._filter_profiles = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        layout = QHBoxLayout()
        outer.addLayout(layout)
        layout.addWidget(QLabel("OCR-sablon:"))
        self.combo = QComboBox()
        self.combo.setMinimumContentsLength(16)
        self.combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        layout.addWidget(self.combo, 1)
        self.edit_button = QPushButton("Szerkesztés…")
        self.edit_button.clicked.connect(self._edit)
        layout.addWidget(self.edit_button)
        self.new_button = QPushButton("Új sablon…")
        self.new_button.clicked.connect(lambda: self._edit(new=True))
        layout.addWidget(self.new_button)
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        outer.addWidget(self.warning_label)
        self.combo.currentIndexChanged.connect(
            lambda: self.edit_button.setEnabled(self.selected_template() is not None),
        )
        self.refresh()

    def selected_template(self) -> OcrTemplate | None:
        return self.combo.currentData()

    def set_document_paths(self, paths):
        self.document_paths = [Path(path) for path in paths]

    def set_authority(self, authority: str, *, filter_profiles: bool = False):
        """Prefer matching profiles; optionally show only this authority's profiles."""
        self._authority = authority.strip()
        self._filter_profiles = filter_profiles
        self.refresh()

    @staticmethod
    def _authority_key(value: str) -> str:
        return " ".join(value.casefold().split())

    def refresh(self, selected_id: str | None = None):
        current = self.selected_template()
        selected_id = selected_id or (current.id if current else None)
        self.combo.clear()
        self.combo.addItem("Automatikus (sablon nélkül)", None)
        try:
            templates = self.store.load()
        except OSError as exc:
            templates = []
            self.store.warnings = [str(exc)]
        authority_key = self._authority_key(self._authority)
        if authority_key:
            if self._filter_profiles:
                templates = [
                    template for template in templates
                    if self._authority_key(template.authority) == authority_key
                ]
            else:
                templates.sort(key=lambda t: self._authority_key(t.authority) != authority_key)
        for template in templates:
            self.combo.addItem(f"{template.authority} — {template.name}", template)
            if template.id == selected_id:
                self.combo.setCurrentIndex(self.combo.count() - 1)
        self.edit_button.setEnabled(self.selected_template() is not None)
        tooltip = (
            "A sablon csak a kijelölt területeket olvassa. Más elrendezéshez válasszon másik sablont. "
            "Az eredményt mindig ellenőrizze."
        )
        if self.store.warnings:
            tooltip += "\nNem tölthető sablonok:\n" + "\n".join(self.store.warnings)
        notices = []
        if self.store.warnings:
            notices.append("Néhány sablon nem tölthető be. Részletek a sablonválasztó súgójában.")
        if authority_key and self._filter_profiles and not templates:
            notices.append("Ehhez a hatósághoz még nincs sablon. Új sablon létrehozható.")
        self.warning_label.setText("\n".join(notices))
        self.warning_label.setVisible(bool(notices))
        self.combo.setToolTip(tooltip)

    def _edit(self, checked=False, *, new=False):
        editor = OcrTemplateEditor(
            self.store, None if new else self.selected_template(), self.document_paths, self,
            authority=self._authority,
        )
        if editor.exec() == QDialog.Accepted:
            self.refresh(editor.saved_template.id if editor.saved_template else None)
