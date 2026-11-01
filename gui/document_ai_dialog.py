from __future__ import annotations

import base64
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.document_ai import (
    AiCancelled, AiClient, AiError, AiResult, FIELD_LABELS, FIELDS, prepare_document,
)

logger = logging.getLogger(__name__)


class AiWorker(QThread):
    result_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, client: AiClient, path: Path, images: list[str] | None = None, parent=None):
        super().__init__(parent)
        self.client, self.path, self.images = client, path, images

    def run(self):
        try:
            if self.isInterruptionRequested():
                return
            if self.images is None:
                images = prepare_document(self.path, self.isInterruptionRequested)
                if self.isInterruptionRequested():
                    return
                result = (images, self.client.ai_capabilities())
            else:
                result = self.client.ai_extract(self.images, consent=True)
            if not self.isInterruptionRequested():
                self.result_ready.emit(result)
        except AiCancelled:
            return
        except AiError as exc:
            logger.warning("AI workflow failed: %s", exc)
            if not self.isInterruptionRequested():
                self.failed.emit(str(exc))
        except Exception as exc:
            # Do not log provider bodies, documents, or API credentials at this UI boundary.
            logger.error("Unexpected AI workflow failure: %s", type(exc).__name__)
            if not self.isInterruptionRequested():
                self.failed.emit("Az AI-feldolgozás váratlan hibával leállt. Az adatlap nem változott.")


class DocumentAiDialog(QDialog):
    def __init__(self, path: Path, client: AiClient, parent=None):
        super().__init__(parent)
        self.path, self.client = path, client
        self.images: list[str] = []
        self.result_data: AiResult | None = None
        self._worker: AiWorker | None = None
        self._closing = False
        self._enabled = False
        self.checks: dict[str, QCheckBox] = {}
        self.setWindowTitle("AI-kitöltés – ellenőrzés és jóváhagyás")
        self.resize(1200, 800)
        layout = QVBoxLayout(self)
        name_label = QLabel(f"Kijelölt dokumentum: {path.name}")
        name_label.setTextFormat(Qt.PlainText)
        layout.addWidget(name_label)
        notice = QLabel(
            "A küldés a dokumentum ÖSSZES oldalképét továbbítja az OpenAI API-nak; használati díjjal járhat. "
            "Csak külső feldolgozásra jóváhagyott, nem bizalmas dokumentumot küldjön. "
            "Az AI tévedhet: a javaslatot és az idézetet az eredeti oldalon ellenőrizze. "
            "Nincs automatikus adatlapmentés."
        )
        notice.setWordWrap(True)
        layout.addWidget(notice)
        splitter = QSplitter(Qt.Horizontal)
        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        navigation = QHBoxLayout()
        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 1)
        self.page_spin.setPrefix("Oldal: ")
        self.zoom = QSpinBox()
        self.zoom.setRange(25, 150)
        self.zoom.setValue(50)
        self.zoom.setSuffix("%")
        navigation.addWidget(self.page_spin)
        navigation.addWidget(QLabel("Nagyítás:"))
        navigation.addWidget(self.zoom)
        preview_layout.addLayout(navigation)
        scroll = QScrollArea()
        self.page_image = QLabel()
        scroll.setWidget(self.page_image)
        preview_layout.addWidget(scroll)
        splitter.addWidget(preview)
        self.table = QTableWidget(4, 5)
        self.table.setHorizontalHeaderLabels(["Átveszem", "Mező", "Javaslat", "Forrásidézet", "Állapot / megjegyzés"])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setWordWrap(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 65)
        self.table.setColumnWidth(1, 105)
        self.table.setColumnWidth(2, 130)
        self.table.setColumnWidth(3, 180)
        for row, field in enumerate(FIELDS):
            check = QCheckBox()
            check.setEnabled(False)
            check.toggled.connect(self._update_buttons)
            self.checks[field] = check
            self.table.setCellWidget(row, 0, check)
            self.table.setItem(row, 1, QTableWidgetItem(FIELD_LABELS[field]))
        splitter.addWidget(self.table)
        splitter.setSizes([500, 700])
        layout.addWidget(splitter, 1)
        self.status = QLabel("Oldalak előkészítése helyben… Még nincs külső feltöltés.")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.consent = QCheckBox("Jóváhagyom e dokumentum oldalképeinek elküldését az OpenAI részére.")
        layout.addWidget(self.consent)
        buttons = QHBoxLayout()
        self.send_btn = QPushButton("Küldés az OpenAI-nak")
        self.apply_btn = QPushButton("Kijelölt javaslatok átvétele")
        self.cancel_btn = QPushButton("Mégse")
        buttons.addWidget(self.send_btn)
        buttons.addStretch()
        buttons.addWidget(self.apply_btn)
        buttons.addWidget(self.cancel_btn)
        layout.addLayout(buttons)
        self.page_spin.valueChanged.connect(self._show_page)
        self.zoom.valueChanged.connect(self._show_page)
        self.table.currentCellChanged.connect(self._source_selected)
        self.consent.toggled.connect(self._update_buttons)
        self.send_btn.clicked.connect(self._send)
        self.apply_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        self._start_worker()

    def _start_worker(self, images: list[str] | None = None):
        self._worker = AiWorker(self.client, self.path, images, self)
        self._worker.result_ready.connect(self._received)
        self._worker.failed.connect(self.status.setText)
        self._worker.finished.connect(self._finished)
        self._update_buttons()
        self._worker.start()

    def _received(self, result):
        if self._closing:
            return
        if isinstance(result, AiResult):
            self.result_data = result
            for row, field in enumerate(FIELDS):
                value = getattr(result, field)
                self.checks[field].setChecked(False)
                self.checks[field].setEnabled(value.status == "found")
                display = {"indefinite": "Határozatlan", "until_modified": "Változtatásig"}.get(value.value, value.value)
                self.table.setItem(row, 2, QTableWidgetItem(display or "—"))
                self.table.setItem(row, 3, QTableWidgetItem(
                    f"{value.page}. oldal: {value.evidence}" if value.page else "Nincs igazolt forrás",
                ))
                state = {"found": "Ellenőrizendő találat", "missing": "Nem található", "uncertain": "Bizonytalan"}
                self.table.setItem(row, 4, QTableWidgetItem(f"{state[value.status]}\n{value.note}"))
            self.table.resizeRowsToContents()
            self.status.setText("Ellenőrizze az oldalképet és az idézeteket, majd jelölje ki az átveendő mezőket. Semmit nem mentettünk.")
        else:
            self.images, capabilities = result
            self._enabled = capabilities.get("enabled") is True
            self.page_spin.setMaximum(len(self.images))
            self._show_page()
            if self._enabled:
                self.status.setText(
                    f"{len(self.images)} oldal előkészítve. Modell: {capabilities.get('model', '')}. "
                    "Feltöltés csak a jelölőnégyzet és a Küldés gomb jóváhagyásával.",
                )
            else:
                self.status.setText(
                    "Az AI nincs beállítva. Az OPENAI_API_KEY a központi szerveren, "
                    "helyi üzemnél ezen a gépen szükséges. Nem történt feltöltés.",
                )

    def _show_page(self, *_args):
        if not self.images:
            return
        pixmap = QPixmap()
        pixmap.loadFromData(base64.b64decode(self.images[self.page_spin.value() - 1]), "JPEG")
        scaled = pixmap.scaledToWidth(max(1, pixmap.width() * self.zoom.value() // 100), Qt.SmoothTransformation)
        self.page_image.setPixmap(scaled)
        self.page_image.resize(scaled.size())

    def _source_selected(self, row, *_args):
        if self.result_data is not None and 0 <= row < len(FIELDS):
            page = getattr(self.result_data, FIELDS[row]).page
            if page is not None:
                self.page_spin.setValue(page)

    def _update_buttons(self, *_args):
        idle = self._worker is None and not self._closing
        self.consent.setEnabled(idle and self._enabled and bool(self.images))
        self.send_btn.setEnabled(idle and self._enabled and bool(self.images) and self.consent.isChecked())
        self.apply_btn.setEnabled(idle and any(check.isEnabled() and check.isChecked() for check in self.checks.values()))

    def _send(self):
        if not self.send_btn.isEnabled():
            return
        self.result_data = None
        for row, field in enumerate(FIELDS):
            self.checks[field].setChecked(False)
            self.checks[field].setEnabled(False)
            for column in range(2, 5):
                self.table.setItem(row, column, QTableWidgetItem(""))
        self.consent.setChecked(False)
        self.status.setText("OpenAI-feldolgozás folyamatban… Az elküldött kérés nem vonható vissza; nincs automatikus újrapróbálás.")
        self._start_worker(self.images)

    def selected_fields(self) -> dict[str, str]:
        if self.result_data is None:
            return {}
        return {
            name: field.value for name in FIELDS
            if (field := getattr(self.result_data, name)).status == "found" and field.value is not None
            and self.checks[name].isChecked()
        }

    def _finished(self):
        worker = self._worker
        self._worker = None
        if worker:
            worker.deleteLater()
        if self._closing:
            super().done(QDialog.Rejected)
        else:
            self._update_buttons()

    def done(self, result):
        if self._worker is not None:
            if result == QDialog.Rejected:
                self._closing = True
                self._worker.requestInterruption()
                self.status.setText("Megszakítás kérve. A már elküldött kérés nem vonható vissza; megvárjuk az időkorlátos művelet végét.")
                self._update_buttons()
            return
        if result == QDialog.Accepted and not self.selected_fields():
            return
        super().done(result)

    def closeEvent(self, event):
        if self._worker is not None:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)
