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

from core.ocr_templates import (
    FIELD_LABELS, OcrTemplate, PDFIUM_LOCK, document_page_count, render_document_page,
)

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


def _fold(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(c)
    )


def extract_fields(text: str) -> DocumentFields:
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


def extract_region_fields(field: str, text: str) -> DocumentFields:
    """A selected area supplies the field label, but never supplies missing evidence."""
    value = unicodedata.normalize("NFC", text).strip()
    if not value:
        return DocumentFields(warnings=("A kijelölt területen nem található olvasható szöveg.",))
    if field == "subject":
        value = re.sub(r"(?i)^\s*t[áa]rgy\s*:\s*", "", value).strip()
        if _LABEL.search(value) or any(_NEXT_FIELD.match(line) for line in value.splitlines()):
            return DocumentFields(warnings=("A tárgy területe más mezőt is tartalmaz; szűkítse a kijelölést.",))
        subject = " ".join(value.split())
        if not subject or len(subject) > 200:
            return DocumentFields(warnings=("A kijelölt tárgy üres vagy túl hosszú; ellenőrizze kézzel.",))
        return DocumentFields(subject=subject)
    if field != "validity":
        raise ValueError("Ismeretlen sablonmező.")
    value = re.sub(
        r"(?i)^\s*(?:hat[áa]lyos|[ée]rv[ée]nyes|hat[áa]ly|lej[áa]rat)\s*:\s*", "", value
    )
    normalized = _fold(value)
    has_date = _DATE.search(normalized) is not None
    indefinite = re.search(r"\bhatarozatlan\b", normalized) is not None
    until_modified = re.search(r"\b(?:visszavonasig|valtoztatasig|modositasig)\b", normalized) is not None
    # Mixed conditions, unrelated labels and alternatives require human review.
    if (
        ":" in value
        or sum((has_date, indefinite, until_modified)) > 1
        or re.search(r"\b(?:vagy|kiveve|felt[eé]ve|nem)\b", normalized)
    ):
        return DocumentFields(warnings=("A kijelölt hatály nem egyértelmű; adja meg kézzel.",))
    if indefinite or until_modified:
        if not re.fullmatch(
            r"(?:hatarozatlan(?:\s+(?:ideig|idore))?|visszavonasig|valtoztatasig|modositasig)[\s.]*",
            normalized,
        ):
            return DocumentFields(warnings=("A kijelölt hatály feltételeket tartalmaz; ellenőrizze kézzel.",))
    elif has_date:
        remainder = _DATE.sub("", normalized)
        remainder = re.sub(r"\b(?:tol|ig|napjaig|napjatol)\b", "", remainder)
        if re.search(r"[^\s./–—-]", remainder):
            return DocumentFields(warnings=("A kijelölt dátumhoz más szöveg tartozik; ellenőrizze kézzel.",))
    return extract_fields("Érvényes: " + value)


def read_document(
    path: Path, cancelled: Callable[[], bool] = lambda: False,
    *, template: OcrTemplate | None = None,
) -> DocumentFields:
    """Read labelled text automatically, or OCR only a template's explicit regions."""
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

    def ocr(image, *, region: bool = False) -> str:
        check_cancelled()
        with ImageOps.exif_transpose(image) as oriented:
            with oriented.convert("RGB") as prepared:
                prepared.thumbnail((3500, 3500))
                options = {"config": "--psm 6"} if region else {}
                text = pytesseract.image_to_string(prepared, lang="hun", timeout=45, **options)
                check_cancelled()
                return text

    try:
        check_cancelled()
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise DocumentReadError("Ehhez a fájltípushoz nincs OCR-támogatás.")
        results = []
        if template is not None:
            unsafe_fields = set()
            page_count = document_page_count(path)
            for page_number in sorted({r.page for r in template.regions}):
                check_cancelled()
                regions = [r for r in template.regions if r.page == page_number]
                if page_number > page_count:
                    unsafe_fields.update(r.field for r in regions)
                    results.append(DocumentFields(warnings=(
                        f"A sablon {page_number}. oldala hiányzik; az ott kijelölt mezők nem olvashatók.",
                    )))
                    continue
                with render_document_page(path, page_number, 3500) as image:
                    for region in regions:
                        check_cancelled()
                        with image.crop(region.pixel_box(*image.size)) as cropped:
                            try:
                                fields = extract_region_fields(region.field, ocr(cropped, region=True))
                            except DocumentReadCancelled:
                                raise
                            except (OSError, RuntimeError) as exc:
                                fields = DocumentFields(warnings=(
                                    f"A kijelölt mező OCR-je sikertelen: {exc}",
                                ))
                            if (
                                (region.field == "subject" and not fields.subject)
                                or (region.field == "validity" and not fields.validity)
                            ):
                                unsafe_fields.add(region.field)
                            results.append(DocumentFields(
                                fields.subject, fields.expiry, fields.validity,
                                tuple(
                                    f"{page_number}. oldal – {FIELD_LABELS[region.field]}: {warning}"
                                    for warning in fields.warnings
                                ),
                            ))
            check_cancelled()
            merged = merge_fields(results)
            return DocumentFields(
                None if "subject" in unsafe_fields else merged.subject,
                None if "validity" in unsafe_fields else merged.expiry,
                None if "validity" in unsafe_fields else merged.validity,
                merged.warnings,
            )
        if path.suffix.lower() == ".pdf":
            with PDFIUM_LOCK, closing(pdfium.PdfDocument(str(path))) as document:
                for index in range(len(document)):
                    check_cancelled()
                    with closing(document[index]) as page:
                        with closing(page.get_textpage()) as textpage:
                            text = textpage.get_text_range()
                        fields = extract_fields(text)
                        # Covers scanned and mixed pages (e.g. a text footer over a scan).
                        has_images = any(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]))
                        if has_images and (not fields.subject or not fields.validity):
                            scale = min(3, 3500 / max(page.get_size()))
                            try:
                                with closing(page.render(scale=scale)) as bitmap:
                                    with bitmap.to_pil() as image:
                                        scanned_fields = extract_fields(ocr(image))
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
                    results.append(extract_fields(ocr(frame)))
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
