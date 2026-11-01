"""Local document reading and conservative Hungarian permit field extraction."""
from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import closing
from datetime import date
import os
from pathlib import Path
import re
from typing import Callable
import unicodedata

from core.ocr_templates import (
    FIELD_LABELS, OcrTemplate, PDFIUM_LOCK, render_document_page,
)

SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_LABEL_NAMES = {
    "subject": ("tárgy",),
    "authority": ("kiadó hatóság", "eljáró hatóság", "hatóság neve", "hatóság", "kiadó"),
    "issued_on": ("keltezés", "keltezése", "kelt", "kelte", "kiadás dátuma", "engedély kelte", "határozat kelte"),
    "decision_number": ("határozatszám", "határozat száma", "ügyiratszám", "iktatószám", "ügyszám"),
    "validity": ("hatályos", "érvényes", "érvényesség", "érvényessége", "hatály", "hatálya", "lejárat", "lejárat dátuma"),
}
_ACCENTS = {char: f"[{char}{plain}]" for char, plain in zip("áéíóöőúüű", "aeiooouuu")}
_LABEL = re.compile(
    r"(?im)^[ \t]*(" + "|".join(
        "".join(_ACCENTS.get(char, re.escape(char)) for char in label)
        for names in _LABEL_NAMES.values() for label in names
    ) + r")\s*[:：]"
)
_NEXT_FIELD = re.compile(r"^\s*[^:\n]{1,60}:\s*")
_AUTHORITY_END = r"(?:kormanyhivatal[a]?|vamhivatal|hivatal|hatosag|igazgatosag)"
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
    authority: str | None = None
    issued_on: date | None = None
    decision_number: str | None = None
    text: str = ""
    blocked_fields: frozenset[str] = frozenset()


def _fold(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(c)
    )


def _uncertain(field: str, message: str) -> DocumentFields:
    return DocumentFields(warnings=(message,), blocked_fields=frozenset({field}))


def _dates(value: str):
    matches = list(_DATE.finditer(value))
    dates = []
    for match in matches:
        try:
            if match.group("year"):
                dates.append(date(int(match["year"]), int(match["month"]), int(match["day"])))
            else:
                dates.append(date(int(match["word_year"]), _MONTHS.index(match["word_month"]) + 1,
                                  int(match["word_day"])))
        except ValueError:
            return matches, dates, True
    return matches, dates, False


def _validity(value: str) -> DocumentFields:
    normalized = _fold(value)
    matches, dates, invalid = _dates(normalized)
    indefinite = re.search(r"\bhatarozatlan\b", normalized) is not None
    until_modified = re.search(r"\b(?:visszavonasig|valtoztatasig|modositasig)\b", normalized) is not None
    ambiguous = _uncertain("validity", "A hatály/érvényesség nem egyértelmű; ellenőrizze a lejáratot.")
    if (re.search(r"\b(?:nem|vagy|kiveve|amennyiben|felt[eé]ve|szamitott)\b", normalized)
            or sum((bool(matches), indefinite, until_modified)) > 1):
        return ambiguous
    if indefinite or until_modified:
        return DocumentFields(validity="indefinite" if indefinite else "until_modified")
    is_range = len(matches) == 2 and re.fullmatch(
        r"\s*\.?\s*(?:-tol|-tel|tol)?\s*[-–—]\s*",
        normalized[matches[0].end():matches[1].start()],
    ) is not None
    is_hungarian_range = len(matches) == 2 and re.fullmatch(
        r"\s*\.?\s*-?tol\s*", normalized[matches[0].end():matches[1].start()]
    ) is not None and re.match(r"\s*\.?\s*-?ig\b", normalized[matches[1].end():]) is not None
    if invalid or not (len(dates) == 1 or (
        len(dates) == 2 and (is_range or is_hungarian_range) and dates[0] <= dates[1]
    )):
        return ambiguous
    if len(dates) == 1:
        if re.match(r"\s*\.?\s*(?:-?tol\b|napjatol\b)", normalized[matches[0].end():]):
            return _uncertain("validity", "Csak kezdődátum található; a lejáratot kézzel kell megadni.")
        if re.search(r"\b(?:ev(?:ig|re)?|honap(?:ig|ra)?|kezdet\w*|hatalyba\w*|felulvizsgalat)\b", normalized):
            return ambiguous
    return DocumentFields(expiry=dates[-1], validity="fixed")


def _label_field(label: str) -> str:
    normalized = _fold(label)
    return next(key for key, names in _LABEL_NAMES.items() if normalized in {_fold(name) for name in names})


def _parse_value(field: str, value: str) -> DocumentFields:
    if not value.strip():
        return DocumentFields()
    if field == "validity":
        return _validity(value)
    if field == "issued_on":
        _, dates, invalid = _dates(_fold(value))
        if invalid or len(set(dates)) != 1:
            return _uncertain(field, "A keltezés nem egyértelmű; ellenőrizze kézzel.")
        return DocumentFields(issued_on=dates[0])
    if field == "authority":
        lines = value.splitlines()
        parts = [lines[0].strip()]
        for line in lines[1:]:
            if not re.search(r"(?:" + _AUTHORITY_END + r"|foosztaly|osztaly)\s*$", _fold(line)):
                break
            parts.append(line.strip())
        value = " ".join(parts)
    else:
        value = " ".join(value.split())
    if len(value) > 200:
        return _uncertain(field, f"{FIELD_LABELS[field]}: túl hosszú találat; adja meg kézzel.")
    if field == "decision_number" and not re.fullmatch(r"(?=.*\d)[\w./()-]+", value):
        return _uncertain(field, "A határozatszám nem egyértelmű; adja meg kézzel.")
    return DocumentFields(**{field: value})


def extract_fields(text: str) -> DocumentFields:
    """Use explicit labels and narrowly recognizable header/closing/validity forms."""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    labels = list(_LABEL.finditer(text))
    results = []
    labelled = set()
    for index, label in enumerate(labels):
        field = _label_field(label.group(1))
        labelled.add(field)
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        lines = text[label.end():end].strip().splitlines()
        value_lines = []
        for line in lines:
            if (not line.strip() or _NEXT_FIELD.match(line)
                    or _fold(line.strip().rstrip(":")) in ("hatarozat", "indokolas", "rendelkezo resz", "jogorvoslat")):
                break
            if value_lines and re.match(r"^(?:a|az|jelen|ezen)\s+(?:engedely|hatarozat)\b", _fold(line)):
                break
            value_lines.append(line.strip())
        results.append(_parse_value(field, "\n".join(value_lines)))
    if "authority" not in labelled:
        header = []
        for line in text.splitlines()[:40]:
            normalized = _fold(line.strip())
            if (_LABEL.match(line) or re.match(r"^(?:ugyfel|kerelmezo|cimzett)\b", normalized)
                    or normalized in ("hatarozat", "indokolas")):
                break
            header.append(line.strip())
        for index, line in enumerate(header):
            normalized = _fold(line)
            if re.fullmatch(r"[a-z .,'–—-]*" + _AUTHORITY_END, normalized):
                start = index
                if re.fullmatch(_AUTHORITY_END, normalized):
                    if index == 0 or not re.search(
                        r"(?:varmegyei|megyei|fovarosi|fovaros|nemzeti|orszagos|teruleti|varosi|jarasi|ado- es)$",
                        _fold(header[index - 1]),
                    ):
                        continue
                    start -= 1
                results.append(_parse_value("authority", "\n".join(header[start:index + 3])))
    if "issued_on" not in labelled:
        for line in text.splitlines():
            normalized = _fold(line.strip())
            matches, dates, invalid = _dates(normalized)
            if len(matches) != 1 or invalid:
                continue
            prefix, suffix = normalized[:matches[0].start()], normalized[matches[0].end():]
            if (re.fullmatch(r"[a-z][a-z .'-]{1,60},\s*", prefix)
                    and not re.search(r"kerelem|hataly|ervenyes|felulvizsgalat|jogero|hatarido", prefix)
                    and re.fullmatch(r"\s*\.?\s*", suffix)):
                results.append(DocumentFields(issued_on=dates[0]))
    chunks = list(text.splitlines()) + [" ".join(p.split()) for p in re.split(r"\n\s*\n", text)]
    for chunk in dict.fromkeys(chunks):
        match = re.match(r"^(?:a|az|jelen|ezen)\s+(?:engedely|hatarozat)(?:\s+(?:hatalya|ervenyessege))?\s+(.+)$",
                         _fold(chunk.strip()))
        if not match:
            continue
        value = match.group(1)
        if re.search(r"felulvizsgalat|korabbi|hivatkozott|kerelem|keltezes|jogero", value):
            continue
        if re.search(r"\b(?:ervenyes|hatalyos|hatarozatlan(?: ideig)?|visszavonasig|valtoztatasig|modositasig)[.!]?$", value):
            results.append(_validity(value))
    return merge_fields(results)


def merge_fields(results: list[DocumentFields]) -> DocumentFields:
    warnings = [warning for result in results for warning in result.warnings]
    blocked = set().union(*(result.blocked_fields for result in results))
    values = {}
    for field in FIELD_LABELS:
        candidates = {}
        for result in results:
            value = (result.validity, result.expiry) if field == "validity" and result.validity else getattr(result, field)
            if value is not None:
                key = _fold(" ".join(value.split())) if isinstance(value, str) else value
                candidates.setdefault(key, value)
        if len(candidates) > 1:
            blocked.add(field)
            warnings.append(f"Több eltérő találat: {FIELD_LABELS[field]}; adja meg kézzel.")
        values[field] = next(iter(candidates.values())) if len(candidates) == 1 and field not in blocked else None
    validity, expiry = values.pop("validity") or (None, None)
    if expiry and values["issued_on"] and expiry < values["issued_on"]:
        blocked.add("validity")
        validity, expiry = None, None
        warnings.append("A lejárat korábbi a keltezésnél; ellenőrizze kézzel.")
    return DocumentFields(
        **values, validity=validity, expiry=expiry,
        warnings=tuple(dict.fromkeys(warnings)), blocked_fields=frozenset(blocked),
        text="\n\n".join(dict.fromkeys(result.text for result in results if result.text)),
    )


def extract_region_fields(field: str, text: str) -> DocumentFields:
    """A selected area supplies the field label, but never supplies missing evidence."""
    value = unicodedata.normalize("NFC", text).strip()
    if not value:
        return _uncertain(field, "A kijelölt területen nem található olvasható szöveg.")
    if field in ("authority", "issued_on", "decision_number"):
        label = _LABEL.match(value)
        if label:
            if _label_field(label.group(1)) != field:
                return _uncertain(field, "A kijelölt terület más mezőt tartalmaz; szűkítse a kijelölést.")
            value = value[label.end():].strip()
        if ":" in value:
            return _uncertain(field, "A kijelölt terület több mezőt tartalmaz; ellenőrizze kézzel.")
        return _parse_value(field, value)
    if field == "subject":
        value = re.sub(r"(?i)^\s*t[áa]rgy\s*:\s*", "", value).strip()
        if _LABEL.search(value) or any(_NEXT_FIELD.match(line) for line in value.splitlines()):
            return _uncertain(field, "A tárgy területe más mezőt is tartalmaz; szűkítse a kijelölést.")
        subject = " ".join(value.split())
        if not subject or len(subject) > 200:
            return _uncertain(field, "A kijelölt tárgy üres vagy túl hosszú; ellenőrizze kézzel.")
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
        return _uncertain(field, "A kijelölt hatály nem egyértelmű; adja meg kézzel.")
    if indefinite or until_modified:
        if not re.fullmatch(
            r"(?:hatarozatlan(?:\s+(?:ideig|idore))?|visszavonasig|valtoztatasig|modositasig)[\s.]*",
            normalized,
        ):
            return _uncertain(field, "A kijelölt hatály feltételeket tartalmaz; ellenőrizze kézzel.")
    elif has_date:
        remainder = _DATE.sub("", normalized)
        remainder = re.sub(r"\b(?:tol|ig|napjaig|napjatol)\b", "", remainder)
        if re.search(r"[^\s./–—-]", remainder):
            return _uncertain(field, "A kijelölt dátumhoz más szöveg tartozik; ellenőrizze kézzel.")
    return extract_fields("Érvényes: " + value)


def read_document(
    path: Path, cancelled: Callable[[], bool] = lambda: False,
    *, template: OcrTemplate | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> DocumentFields:
    """Read every page. Optional template regions supplement, never replace, that pass."""
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
        native_pages, scanned_pages, transcript, failures = [], [], [], []

        def page_failed(number: int, exc: Exception) -> None:
            message = f"{number}. oldal: a teljes felismerés sikertelen ({exc}). Ellenőrizze az eredeti dokumentumot."
            failures.append(message)
            results.append(DocumentFields(warnings=(message,)))
            transcript.append(message)

        if path.suffix.lower() == ".pdf":
            with PDFIUM_LOCK, closing(pdfium.PdfDocument(str(path))) as document:
                page_count = len(document)
                if not page_count:
                    raise DocumentReadError("A PDF nem tartalmaz feldolgozható oldalt.")
                for index in range(page_count):
                    check_cancelled()
                    if progress:
                        progress(index + 1, page_count)
                    text, scanned = "", ""
                    try:
                        with closing(document[index]) as page:
                            with closing(page.get_textpage()) as textpage:
                                text = textpage.get_text_range()
                            has_images = any(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]))
                            # Do not skip images just because two fields were already found.
                            if has_images or not text.strip():
                                scale = min(3, 3500 / max(page.get_size()))
                                with closing(page.render(scale=scale)) as bitmap:
                                    with bitmap.to_pil() as image:
                                        scanned = ocr(image)
                    except DocumentReadCancelled:
                        raise
                    except (OSError, RuntimeError, pdfium.PdfiumError) as exc:
                        page_failed(index + 1, exc)
                    native_pages.append(text)
                    scanned_pages.append(scanned)
                    transcript.append(f"--- {index + 1}. oldal ---")
                    if text.strip():
                        transcript.append("PDF-szöveg:\n" + text)
                    if scanned.strip():
                        transcript.append("OCR:\n" + scanned)
        else:
            with Image.open(path) as image:
                page_count = getattr(image, "n_frames", 1)
                for index, frame in enumerate(ImageSequence.Iterator(image), 1):
                    check_cancelled()
                    if progress:
                        progress(index, page_count)
                    try:
                        scanned = ocr(frame)
                    except DocumentReadCancelled:
                        raise
                    except (OSError, RuntimeError) as exc:
                        page_failed(index, exc)
                        scanned = ""
                    scanned_pages.append(scanned)
                    transcript.append(f"--- {index}. oldal ---\n{scanned}")
        results.extend((extract_fields("\n\n".join(native_pages)), extract_fields("\n\n".join(scanned_pages))))
        if template is not None:
            for page_number in sorted({r.page for r in template.regions}):
                check_cancelled()
                regions = [r for r in template.regions if r.page == page_number]
                if page_number > page_count:
                    for region in regions:
                        results.append(_uncertain(region.field, f"A sablon {page_number}. oldala hiányzik."))
                    continue
                with render_document_page(path, page_number, 3500) as image:
                    for region in regions:
                        check_cancelled()
                        with image.crop(region.pixel_box(*image.size)) as cropped:
                            try:
                                text = ocr(cropped, region=True)
                                transcript.append(f"--- {page_number}. oldal, kijelölt {FIELD_LABELS[region.field]} ---\n{text}")
                                fields = extract_region_fields(region.field, text)
                            except DocumentReadCancelled:
                                raise
                            except (OSError, RuntimeError) as exc:
                                fields = _uncertain(region.field, f"A kijelölt mező OCR-je sikertelen: {exc}")
                            results.append(replace(fields, warnings=tuple(
                                f"{page_number}. oldal – {FIELD_LABELS[region.field]}: {warning}"
                                for warning in fields.warnings
                            )))
        check_cancelled()
        merged = merge_fields(results)
        if failures and not any(text.strip() for text in (*native_pages, *scanned_pages)) and not any(
            getattr(merged, field) for field in FIELD_LABELS
        ):
            raise DocumentReadError("\n".join(failures))
        return replace(merged, text=f"{path.name}\n\n" + "\n\n".join(transcript))
    except DocumentReadCancelled:
        raise
    except DocumentReadError:
        raise
    except (OSError, ValueError, RuntimeError, pdfium.PdfiumError, Image.DecompressionBombError) as exc:
        raise DocumentReadError(
            f"A dokumentum nem olvasható: {exc}\n"
            "OCR-hez Tesseract és a magyar (hun) nyelvi csomag szükséges."
        ) from exc
