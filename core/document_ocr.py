"""Local document reading and conservative Hungarian permit field extraction."""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import closing
from datetime import date
import os
from pathlib import Path
import re
from typing import Callable
import unicodedata


SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_LABEL = re.compile(r"(?i)\b(tárgy|targy|hatályos|hatalyos|érvényes|ervenyes)\s*:")
_NEXT_FIELD = re.compile(r"^\s*[^:\n]{1,60}:\s*")
_MONTHS = (
    "januar", "februar", "marcius", "aprilis", "majus", "junius",
    "julius", "augusztus", "szeptember", "oktober", "november", "december",
)
_DATE = re.compile(
    r"(?<!\d)(?P<year>\d{4})\s*[./-]\s*"
    r"(?P<month>\d{1,2})\s*[./-]\s*(?P<day>\d{1,2})(?!\d)"
    r"|(?<!\d)(?P<word_year>\d{4})\.?\s+"
    r"(?P<word_month>" + "|".join(_MONTHS) + r")\s+(?P<word_day>\d{1,2})(?!\d)"
)


class DocumentReadError(RuntimeError):
    pass


class DocumentReadCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class DocumentFields:
    subject: str | None = None
    expiry: date | None = None
    validity: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtractionRule:
    start: str = ""
    end: str = ""

    def __post_init__(self):
        if not isinstance(self.start, str) or not isinstance(self.end, str):
            raise ValueError("A keresőkifejezéseknek szövegnek kell lenniük.")
        if self.end.strip() and not self.start.strip():
            raise ValueError("Záró kifejezéshez kezdő kifejezés is szükséges.")


@dataclass(frozen=True)
class OcrRules:
    subject: ExtractionRule = ExtractionRule()
    validity: ExtractionRule = ExtractionRule()


def _fold(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(c)
    )


def _extract_default_fields(text: str) -> DocumentFields:
    """Only consume labelled values; never infer expiry from unrelated dates."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    labels = list(_LABEL.finditer(text))
    subjects: set[str] = set()
    validities: set[tuple[str, date | None]] = set()
    warnings: list[str] = []
    for index, label in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        lines = text[label.end():end].strip().splitlines()
        value_lines = []
        for line in lines:
            if not line.strip() or _NEXT_FIELD.match(line):
                break
            value_lines.append(line.strip())
        value = " ".join(value_lines)
        if _fold(label.group(1)) == "targy":
            if value:
                subjects.add(value)
            continue
        normalized = _fold(value)
        if re.search(r"\bhatarozatlan(?:\s+ideig)?\b", normalized):
            validities.add(("indefinite", None))
            continue
        if re.search(r"\b(?:visszavonasig|valtoztatasig|modositasig)\b", normalized):
            validities.add(("until_modified", None))
            continue
        matches = list(_DATE.finditer(normalized))
        dates = []
        invalid = False
        for match in matches:
            try:
                if match.group("year"):
                    parsed = date(int(match["year"]), int(match["month"]), int(match["day"]))
                else:
                    parsed = date(
                        int(match["word_year"]), _MONTHS.index(match["word_month"]) + 1,
                        int(match["word_day"]),
                    )
                dates.append(parsed)
            except ValueError:
                invalid = True
        # Two dates are only safe as an explicitly delimited validity interval.
        is_range = len(matches) == 2 and re.fullmatch(
            r"\s*\.?\s*(?:-tol|-tel|tol)?\s*[-–—]\s*",
            normalized[matches[0].end():matches[1].start()],
        ) is not None
        is_hungarian_range = len(matches) == 2 and re.fullmatch(
            r"\s*\.?\s*-?t[oő]l\s*", normalized[matches[0].end():matches[1].start()]
        ) is not None and re.match(r"\s*\.?\s*-?ig\b", normalized[matches[1].end():]) is not None
        if not invalid and (len(dates) == 1 or (
            len(dates) == 2 and (is_range or is_hungarian_range) and dates[0] <= dates[1]
        )):
            # A start date alone is not an expiry.
            tail = normalized[matches[-1].end():]
            if len(dates) == 1 and re.match(r"\s*\.?\s*-?tol\b", tail):
                warnings.append("Csak kezdődátum található; a lejáratot kézzel kell megadni.")
            else:
                validities.add(("fixed", dates[-1]))
        else:
            warnings.append("A hatály/érvényesség nem egyértelmű; ellenőrizze a lejáratot.")
    if len(subjects) > 1:
        warnings.append("Több eltérő tárgy található; a tárgyat kézzel kell megadni.")
    if len(validities) > 1:
        warnings.append("Több eltérő lejárat található; a hatályt kézzel kell megadni.")
    validity, expiry = next(iter(validities)) if len(validities) == 1 else (None, None)
    return DocumentFields(
        subject=next(iter(subjects)) if len(subjects) == 1 else None,
        expiry=expiry, validity=validity, warnings=tuple(dict.fromkeys(warnings)),
    )


def _rule_values(text: str, rule: ExtractionRule) -> tuple[list[str], list[str]]:
    def pattern(marker: str):
        marker = unicodedata.normalize("NFC", marker.strip())
        escaped = r"\s+".join(re.escape(part) for part in marker.split())
        prefix = r"(?<!\w)" if marker[0].isalnum() else ""
        suffix = r"(?!\w)" if marker[-1].isalnum() else ""
        return re.compile(prefix + escaped + suffix, re.IGNORECASE)

    starts = list(pattern(rule.start).finditer(text))
    end_pattern = pattern(rule.end) if rule.end.strip() else None
    values = []
    warnings = []
    for index, start in enumerate(starts):
        limit = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        remaining = text[start.end():limit]
        if end_pattern:
            end = end_pattern.search(remaining)
            if end is None:
                warnings.append(f"Nem található záró kifejezés: {rule.end}")
                continue
            value = remaining[:end.start()]
        else:
            lines = remaining.lstrip(" \t:;–—-\n").splitlines()
            value_lines = []
            for line in lines:
                if not line.strip() or _NEXT_FIELD.match(line):
                    break
                value_lines.append(line.strip())
            value = " ".join(value_lines)
        value = " ".join(value.strip(" \t:;–—-\n").split())
        if value:
            values.append(value)
    if not starts:
        warnings.append(f"Nem található kezdő kifejezés: {rule.start}")
    elif not values and not warnings:
        warnings.append(f"A keresőkifejezés után nincs érték: {rule.start}")
    return values, warnings


def extract_fields(text: str, rules: OcrRules | None = None) -> DocumentFields:
    """Custom rules replace default recognition only for the configured field."""
    if rules is None or rules == OcrRules():
        return _extract_default_fields(text)
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    # Do not retain warnings or values from the default rule for an overridden field.
    default_labels = list(_LABEL.finditer(text))
    default_parts = []
    for index, label in enumerate(default_labels):
        rule = rules.subject if _fold(label.group(1)) == "targy" else rules.validity
        if not rule.start.strip():
            end = default_labels[index + 1].start() if index + 1 < len(default_labels) else len(text)
            default_parts.append(text[label.start():end])
    defaults = _extract_default_fields("\n\n".join(default_parts))
    subject, expiry, validity = defaults.subject, defaults.expiry, defaults.validity
    warnings = list(defaults.warnings)
    if rules.subject.start.strip():
        values, issues = _rule_values(text, rules.subject)
        warnings.extend(issues)
        unique = set(values)
        subject = next(iter(unique)) if len(unique) == 1 and not issues else None
        if len(unique) > 1:
            warnings.append("Több eltérő tárgy található; a tárgyat kézzel kell megadni.")
    if rules.validity.start.strip():
        values, issues = _rule_values(text, rules.validity)
        warnings.extend(issues)
        parsed = [_extract_default_fields(f"Érvényes: {value}") for value in values]
        merged = merge_fields(parsed)
        warnings.extend(merged.warnings)
        if not issues and all(value.validity for value in parsed):
            expiry, validity = merged.expiry, merged.validity
    return DocumentFields(subject, expiry, validity, tuple(dict.fromkeys(warnings)))


def merge_fields(results: list[DocumentFields]) -> DocumentFields:
    subjects = {r.subject for r in results if r.subject}
    validities = {(r.validity, r.expiry) for r in results if r.validity}
    warnings = [warning for result in results for warning in result.warnings]
    if len(subjects) > 1:
        warnings.append("A fájlok eltérő tárgyat tartalmaznak; válassza ki kézzel a megfelelőt.")
    if len(validities) > 1:
        warnings.append("A fájlok eltérő lejáratot tartalmaznak; válassza ki kézzel a megfelelőt.")
    validity, expiry = next(iter(validities)) if len(validities) == 1 else (None, None)
    return DocumentFields(
        next(iter(subjects)) if len(subjects) == 1 else None,
        expiry, validity, tuple(dict.fromkeys(warnings)),
    )


def read_document(
    path: Path, cancelled: Callable[[], bool] = lambda: False, rules: OcrRules | None = None,
) -> DocumentFields:
    """Read native PDF text first, OCR scanned pages/images with Hungarian Tesseract."""
    try:
        import pypdfium2 as pdfium
        import pytesseract
        from PIL import Image, ImageOps, ImageSequence
    except ImportError as exc:
        raise DocumentReadError("Hiányzó OCR-csomag. Telepítés: pip install -r requirements.txt") from exc

    executable = os.environ.get("TESSERACT_CMD")
    if executable:
        pytesseract.pytesseract.tesseract_cmd = executable

    def check_cancelled() -> None:
        if cancelled():
            raise DocumentReadCancelled()

    def ocr(image) -> str:
        check_cancelled()
        with ImageOps.exif_transpose(image) as oriented:
            with oriented.convert("RGB") as prepared:
                prepared.thumbnail((3500, 3500))
                return pytesseract.image_to_string(prepared, lang="hun", timeout=45)

    try:
        check_cancelled()
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise DocumentReadError("Ehhez a fájltípushoz nincs OCR-támogatás.")
        results = []
        if path.suffix.lower() == ".pdf":
            with closing(pdfium.PdfDocument(str(path))) as document:
                for index in range(len(document)):
                    check_cancelled()
                    with closing(document[index]) as page:
                        with closing(page.get_textpage()) as textpage:
                            text = textpage.get_text_range()
                        fields = extract_fields(text, rules)
                        # Covers scanned and mixed pages (e.g. a text footer over a scan).
                        has_images = any(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]))
                        if has_images and (not fields.subject or not fields.validity):
                            scale = min(3, 3500 / max(page.get_size()))
                            try:
                                with closing(page.render(scale=scale)) as bitmap:
                                    with bitmap.to_pil() as image:
                                        scanned_fields = extract_fields(ocr(image), rules)
                            except (OSError, RuntimeError) as exc:
                                if not text.strip():
                                    raise
                                fields = DocumentFields(
                                    fields.subject, fields.expiry, fields.validity,
                                    (*fields.warnings, f"A kép OCR-felismerése sikertelen: {exc}"),
                                )
                            else:
                                fields = DocumentFields(
                                    fields.subject or scanned_fields.subject,
                                    fields.expiry if fields.validity else scanned_fields.expiry,
                                    fields.validity or scanned_fields.validity,
                                    tuple(dict.fromkeys((*fields.warnings, *scanned_fields.warnings))),
                                )
                        results.append(fields)
        else:
            with Image.open(path) as image:
                for frame in ImageSequence.Iterator(image):
                    results.append(extract_fields(ocr(frame), rules))
        check_cancelled()
        return merge_fields(results)
    except DocumentReadCancelled:
        raise
    except DocumentReadError:
        raise
    except (OSError, ValueError, RuntimeError, pdfium.PdfiumError, Image.DecompressionBombError) as exc:
        raise DocumentReadError(
            f"A dokumentum nem olvasható: {exc}\n"
            "OCR-hez Tesseract és a magyar (hun) nyelvi csomag szükséges."
        ) from exc
