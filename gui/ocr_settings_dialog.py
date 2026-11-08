import json
import logging
from dataclasses import asdict

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGridLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from core.document_ocr import ExtractionRule, OcrRules, extract_fields

logger = logging.getLogger(__name__)


def ocr_settings() -> QSettings:
    return QSettings("MEH", "EngedelyNyilvantarto")


def load_ocr_rules(settings: QSettings | None = None) -> OcrRules:
    settings = settings if settings is not None else ocr_settings()
    raw = settings.value("ocr/rules")
    if settings.status() != QSettings.NoError:
        raise ValueError("Az OCR-beállítások nem olvashatók. Ellenőrizze a helyi beállításfájl hozzáférését.")
    if raw is None:
        return OcrRules()
    try:
        data = json.loads(raw)
        return OcrRules(
            ExtractionRule(**data["subject"]), ExtractionRule(**data["validity"]),
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(
            "A mentett OCR-szabályok hibásak. Nyissa meg az OCR-beállításokat, "
            "majd javítsa vagy állítsa vissza és mentse a szabályokat."
        ) from exc


class OcrSettingsDialog(QDialog):
    def __init__(self, parent=None, settings: QSettings | None = None):
        super().__init__(parent)
        self.settings = settings if settings is not None else ocr_settings()
        self.setWindowTitle("OCR-szabályok")
        self.resize(740, 480)
        layout = QVBoxLayout(self)
        help_text = QLabel(
            "Üres kezdő kifejezés: beépített felismerés. Csak kezdő kifejezés: "
            "szöveg a következő mezőig vagy üres sorig. Kezdő és záró kifejezés: "
            "a kettő közötti érték. A keresés nem érzékeny a kis- és nagybetűkre; "
            "a kifejezések nem reguláris kifejezések."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        grid = QGridLayout()
        for column, title in enumerate(("Mező", "Kezdő kifejezés", "Záró kifejezés (opcionális)")):
            grid.addWidget(QLabel(title), 0, column)
        self.inputs = {}
        for row, (key, label) in enumerate((("subject", "Tárgy"), ("validity", "Érvényesség")), 1):
            start, end = QLineEdit(), QLineEdit()
            start.setPlaceholderText("Tárgy" if key == "subject" else "Érvényes")
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(start, row, 1)
            grid.addWidget(end, row, 2)
            self.inputs[key] = (start, end)
        layout.addLayout(grid)
        self.sample = QPlainTextEdit()
        self.sample.setPlaceholderText("Ide másolhat mintaszöveget a szabályok kipróbálásához.")
        layout.addWidget(self.sample)
        preview = QPushButton("Kipróbálás")
        preview.clicked.connect(self.preview)
        layout.addWidget(preview)
        self.preview_result = QPlainTextEdit()
        self.preview_result.setReadOnly(True)
        layout.addWidget(self.preview_result)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel | QDialogButtonBox.RestoreDefaults,
        )
        buttons.button(QDialogButtonBox.Save).setText("Mentés")
        buttons.button(QDialogButtonBox.Cancel).setText("Mégse")
        reset = buttons.button(QDialogButtonBox.RestoreDefaults)
        reset.setText("Beépített felismerés")
        reset.clicked.connect(lambda: self.set_rules(OcrRules()))
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        try:
            self.set_rules(load_ocr_rules(self.settings))
        except ValueError as exc:
            logger.warning("%s", exc)
            self.preview_result.setPlainText(str(exc))

    def set_rules(self, rules: OcrRules) -> None:
        for key, (start, end) in self.inputs.items():
            rule = getattr(rules, key)
            start.setText(rule.start)
            end.setText(rule.end)

    def get_rules(self) -> OcrRules:
        return OcrRules(**{
            key: ExtractionRule(start.text().strip(), end.text().strip())
            for key, (start, end) in self.inputs.items()
        })

    def preview(self) -> None:
        try:
            fields = extract_fields(self.sample.toPlainText(), self.get_rules())
        except ValueError as exc:
            self.preview_result.setPlainText(str(exc))
            return
        validity = {
            "indefinite": "Határozatlan", "until_modified": "Változtatásig",
        }.get(fields.validity, fields.expiry.strftime("%Y.%m.%d.") if fields.expiry else "Nincs találat")
        self.preview_result.setPlainText("\n".join((
            f"Tárgy: {fields.subject or 'Nincs találat'}",
            f"Határozatszám: {fields.decision_number or 'Nincs találat'}",
            f"Érvényesség: {validity}", *fields.warnings,
        )))

    def save(self) -> None:
        try:
            rules = self.get_rules()
        except ValueError as exc:
            QMessageBox.warning(self, "OCR-szabályok", str(exc))
            return
        self.settings.setValue("ocr/rules", json.dumps(asdict(rules), ensure_ascii=False))
        self.settings.sync()
        if self.settings.status() != QSettings.NoError:
            logger.error("OCR settings could not be saved: %s", self.settings.status())
            QMessageBox.warning(self, "OCR-szabályok", "Nem sikerült menteni az OCR-beállításokat.")
            return
        self.accept()
