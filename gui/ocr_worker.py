from pathlib import Path
import logging

from PySide6.QtCore import QThread, Signal

from core.document_ocr import (
    DocumentReadCancelled, DocumentReadError, OcrRules, merge_fields, read_document,
)

logger = logging.getLogger(__name__)


class OcrWorker(QThread):
    result_ready = Signal(object, object)

    def __init__(self, paths: list[Path], parent=None, rules: OcrRules | None = None):
        super().__init__(parent)
        self.paths = paths
        self.rules = rules

    def run(self) -> None:
        results = []
        errors = []
        for path in self.paths:
            if self.isInterruptionRequested():
                return
            try:
                results.append(read_document(path, self.isInterruptionRequested, self.rules))
            except DocumentReadCancelled:
                return
            except DocumentReadError as exc:
                logger.warning("OCR failed for %s: %s", path.name, exc)
                errors.append(f"{path.name}: {exc}")
        if not self.isInterruptionRequested():
            self.result_ready.emit(merge_fields(results), errors)
