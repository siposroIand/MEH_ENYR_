"""Local document reading and conservative Hungarian permit field extraction."""
from __future__ import annotations

from dataclasses import dataclass
from calendar import monthrange
from contextlib import closing
from datetime import date
import os
from pathlib import Path
import re
from typing import Callable
import unicodedata


SUPPORTED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_LABEL = re.compile(
    r"(?i)\b(tárgy|targy|hatályos|hatalyos|érvényes|ervenyes|lejárat(?:a)?|lejarat(?:a)?|"
    r"érvényesség(?:e|i ideje|i idő)?|ervenyesseg(?:e|i ideje|i ido)?|hatály(?:a)?|hataly(?:a)?)\s*:"
)
_NEXT_FIELD = re.compile(r"^\s*[^:\n]{1,60}:\s*")
_MONTHS = (
    "januar", "februar", "marcius", "aprilis", "majus", "junius",
    "julius", "augusztus", "szeptember", "oktober", "november", "december",
)
_DATE = re.compile(
    r"(?<!\d)(?P<year>\d{4})\s*[./-]\s*"
    r"(?P<month>\d{1,2})\s*[./-]\s*(?P<day>\d{1,2})(?!\d)"
    r"|(?<!\d)(?P<word_year>\d{4})(?:\.\s*|\s+)"
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
    decision_number: str | None = None
    ambiguous_fields: frozenset[str] = frozenset()


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


def _parse_date(match: re.Match) -> date:
    if match.group("year"):
        return date(int(match["year"]), int(match["month"]), int(match["day"]))
    return date(int(match["word_year"]), _MONTHS.index(match["word_month"]) + 1, int(match["word_day"]))


_YEARS = {"egy": 1, "ket": 2, "ketto": 2, "harom": 3, "negy": 4, "ot": 5,
          "hat": 6, "het": 7, "nyolc": 8, "kilenc": 9, "tiz": 10}
_DURATION_PATTERN = r"(?P<years>\d{1,3}|" + "|".join(_YEARS) + r")\s*(?:\([a-z]+\)\s*)?ev(?:ig|re)?\b"
_DURATION = re.compile(r"\b" + _DURATION_PATTERN)
_RELATIVE = re.compile(
    r"\b(?:kiadas(?:a|anak)?(?:tol| napjatol)|keltezes(?:e|enek)?(?:tol| napjatol)|"
    r"keltetol|kelt(?:enek)? napjatol)\s+(?:szamitott\s+)?"
    + _DURATION_PATTERN
)
_ISSUED = re.compile(
    r"(?im)^\s*(?:kelt(?:ezes(?:e)?)?|kiadas(?:anak)?\s*(?:datuma|napja|kelte)|"
    r"hatarozat\s+kelte)\s*:?\s*([^\n\f]+)"
    r"|^\s*[a-z][a-z .-]{1,60},\s*(\d{4}[^\n\f]+)"
)
_DECISION = re.compile(
    r"(?i)^[ \t]*(?:hatarozat[ \t]*(?:szam|szama)|ugyirat[ \t]*(?:szam|szama)|"
    r"iktato[ \t]*(?:szam|szama)|ugyszam|ugy szama|engedely[ \t]*(?:szam|szama)|szam)"
    r"(?:[ \t]*[:：][ \t]*|[ \t]+)(.*)$"
)


def _issued_dates(text: str) -> set[date]:
    dates = set()
    for label in _ISSUED.finditer(_fold(text).replace("\f", "\n")):
        for match in _DATE.finditer(label.group(1) or label.group(2)):
            try:
                dates.add(_parse_date(match))
            except ValueError:
                # An invalid competing dateline must not allow an arbitrary choice.
                return set()
    return dates


def _decision_number(text: str) -> tuple[str | None, list[str]]:
    numbers = set()
    lines = text.replace("\f", "\n\n").splitlines()
    for index, line in enumerate(lines):
        match = _DECISION.match(_fold(line))
        if not match:
            continue
        # Header folding must not shift offsets if another line contains e.g. ß.
        value = line[len(line) - len(match.group(1)):].strip() if match.group(1) else ""
        if not value and index + 1 < len(lines) and not _NEXT_FIELD.match(lines[index + 1]):
            value = lines[index + 1].strip()
        # Stop at another labelled header, but retain spaces/punctuation inside IDs.
        value = re.split(r"\s+(?=[^\W\d_][^:\n]{1,30}:)", value)[0].strip()
        if any(c.isdigit() for c in value) and len(value) <= 200:
            numbers.add(value)
    if len(numbers) > 1:
        return None, ["Több eltérő határozat-/ügyiratszám található; válassza ki kézzel a megfelelőt."]
    return next(iter(numbers), None), []


def _parse_validity(value: str, issued: set[date]) -> tuple[tuple[str, date | None] | None, str | None]:
    normalized = _fold(value)
    relative = list(_RELATIVE.finditer(normalized))
    if relative:
        if re.search(r"\b(?:hatarozatlan|visszavonasig|valtoztatasig|modositasig)\b", normalized):
            return None, "Ellentmondó hatályadatok találhatók; ellenőrizze kézzel."
        durations = {int(m["years"]) if m["years"].isdigit() else _YEARS[m["years"]] for m in _DURATION.finditer(normalized)}
        if len(issued) == 1 and len(durations) == 1 and 0 < next(iter(durations)) <= 100:
            start = next(iter(issued))
            year = start.year + next(iter(durations))
            if year <= 9999:
                expiry = start.replace(year=year, day=min(start.day, monthrange(year, start.month)[1]))
                explicit = list(_DATE.finditer(normalized))
                try:
                    consistent = all(_parse_date(m) == expiry for m in explicit)
                except ValueError:
                    consistent = False
                if consistent:
                    return ("fixed", expiry), None
        return None, "A relatív lejárathoz egyértelmű keltezés és időtartam szükséges; ellenőrizze kézzel."
    # Do not mistake notification/finality dates for the issue date.
    if re.search(r"\b(?:ev|evig|evre|honap|nap)\b", normalized) and re.search(
        r"\b(?:szamitott|jogerore|veglegesse|kezbesites|kezhezvetel)", normalized
    ):
        return None, "A lejárat kezdő eseménye nem a keltezés; ellenőrizze kézzel."
    special = set()
    if re.search(r"\bhatarozatlan(?:\s+ideig)?\b", normalized):
        special.add(("indefinite", None))
    if re.search(r"\b(?:visszavonasig|valtoztatasig|modositasig)\b", normalized):
        special.add(("until_modified", None))
    matches = list(_DATE.finditer(normalized))
    dates = []
    try:
        dates = [_parse_date(match) for match in matches]
    except ValueError:
        return None, "Érvénytelen dátum található; ellenőrizze a lejáratot."
    if special:
        if len(special) == 1 and not dates:
            return next(iter(special)), None
        return None, "Ellentmondó hatályadatok találhatók; ellenőrizze kézzel."
    is_range = len(matches) == 2 and re.fullmatch(
        r"\s*\.?\s*(?:-tol|-tel|tol)?\s*[-–—]\s*",
        normalized[matches[0].end():matches[1].start()],
    ) is not None
    is_hungarian_range = len(matches) == 2 and re.fullmatch(
        r"\s*\.?\s*-?tol\s*", normalized[matches[0].end():matches[1].start()]
    ) is not None and re.match(r"\s*\.?\s*-?ig\b", normalized[matches[1].end():]) is not None
    if len(dates) == 1 or (len(dates) == 2 and (is_range or is_hungarian_range) and dates[0] <= dates[1]):
        tail = normalized[matches[-1].end():]
        if len(dates) == 1 and re.match(r"\s*\.?\s*-?(?:tol|napjatol|kezdodoen)\b", tail):
            return None, "Csak kezdődátum található; a lejáratot kézzel kell megadni."
        return ("fixed", dates[-1]), None
    return None, "A hatály/érvényesség nem egyértelmű; ellenőrizze a lejáratot."


def _prose_validities(text: str) -> list[str]:
    values = []
    for line in _fold(text).splitlines():
        line = re.sub(r"^\s*a\s+", "", line.strip()).rstrip(". ")
        if _RELATIVE.fullmatch(line):
            values.append(line)
    # Restrict unlabelled recognition to permit/decision validity statements.
    for paragraph in re.split(r"\n\s*\n|\f", _fold(text)):
        paragraph = " ".join(paragraph.split())
        for match in re.finditer(
            r"\b(?:engedely|hatarozat)\s+"
            r"(?:(?:ervenyessege|ervenyessegi ideje|ervenyessegi ido|hatalya|hatalyos|ervenyes)\s*:?\s+"
            r"([^:]{1,250})|([^:]{1,180}?)\s+(?:ervenyes|hatalyos)\b)", paragraph,
        ):
            value = match.group(1) or match.group(2)
            # A new sentence may contain unrelated dates or obligations.
            value = re.split(r"(?<=[.!?])\s+(?=(?:az?|ez|jelen|kerem)\b)", value, maxsplit=1)[0]
            values.append(value)
    return values


def _extract_default_fields(text: str, *, subject_enabled: bool = True, validity_enabled: bool = True) -> DocumentFields:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    labels = list(_LABEL.finditer(text))
    subjects: set[str] = set()
    validities: set[tuple[str, date | None]] = set()
    warnings: list[str] = []
    ambiguous = set()
    validity_values = []
    for index, label in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        page_end = text.find("\f", label.end(), end)
        if page_end != -1:
            end = page_end
        lines = text[label.end():end].strip().splitlines()
        value_lines = []
        for line in lines:
            if not line.strip() or _NEXT_FIELD.match(line) or _ISSUED.match(_fold(line)):
                break
            value_lines.append(line.strip())
        value = " ".join(value_lines)
        if _fold(label.group(1)) == "targy":
            if value and subject_enabled:
                subjects.add(value)
            continue
        validity_values.append(value)
    if validity_enabled:
        issued = _issued_dates(text)
        for value in [*validity_values, *_prose_validities(text)]:
            parsed, warning = _parse_validity(value, issued)
            if parsed:
                validities.add(parsed)
            if warning:
                warnings.append(warning)
                ambiguous.add("validity")
    if len(subjects) > 1:
        warnings.append("Több eltérő tárgy található; a tárgyat kézzel kell megadni.")
        ambiguous.add("subject")
    if len(validities) > 1:
        warnings.append("Több eltérő lejárat található; a hatályt kézzel kell megadni.")
        ambiguous.add("validity")
    validity, expiry = next(iter(validities)) if len(validities) == 1 and "validity" not in ambiguous else (None, None)
    decision_number, issues = _decision_number(text)
    warnings.extend(issues)
    if issues:
        ambiguous.add("decision_number")
    return DocumentFields(
        subject=next(iter(subjects)) if len(subjects) == 1 else None,
        expiry=expiry, validity=validity, warnings=tuple(dict.fromkeys(warnings)),
        decision_number=decision_number,
        ambiguous_fields=frozenset(ambiguous),
    )


def _rule_values(text: str, rule: ExtractionRule) -> tuple[list[str], list[str]]:
    def pattern(marker: str):
        marker = unicodedata.normalize("NFC", marker.strip())
        escaped = r"\s+".join(re.escape(part) for part in marker.split())
        prefix = r"(?<!\w)" if marker[0].isalnum() else ""
        suffix = r"(?!\w)" if marker[-1].isalnum() else ""
        return re.compile(prefix + escaped + suffix, re.IGNORECASE)

    if "\f" in text:
        values, warnings = [], []
        for page in text.split("\f"):
            if pattern(rule.start).search(page):
                page_values, page_warnings = _rule_values(page, rule)
                values.extend(page_values)
                warnings.extend(page_warnings)
        if not values and not warnings:
            warnings.append(f"Nem található kezdő kifejezés: {rule.start}")
        return values, warnings

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
    defaults = _extract_default_fields(
        text, subject_enabled=not rules.subject.start.strip(),
        validity_enabled=not rules.validity.start.strip(),
    )
    subject, expiry, validity = defaults.subject, defaults.expiry, defaults.validity
    warnings = list(defaults.warnings)
    ambiguous = set(defaults.ambiguous_fields)
    if rules.subject.start.strip():
        values, issues = _rule_values(text, rules.subject)
        warnings.extend(issues)
        unique = set(values)
        subject = next(iter(unique)) if len(unique) == 1 and not issues else None
        if len(unique) > 1:
            warnings.append("Több eltérő tárgy található; a tárgyat kézzel kell megadni.")
        if issues or len(unique) > 1:
            ambiguous.add("subject")
    if rules.validity.start.strip():
        values, issues = _rule_values(text, rules.validity)
        warnings.extend(issues)
        parsed = [_parse_validity(value, _issued_dates(text)) for value in values]
        warnings.extend(warning for _, warning in parsed if warning)
        candidates = {value for value, _ in parsed if value}
        if not issues and parsed and all(value for value, _ in parsed) and len(candidates) == 1:
            validity, expiry = next(iter(candidates))
        elif len(candidates) > 1:
            warnings.append("Több eltérő lejárat található; a hatályt kézzel kell megadni.")
        if issues or any(warning for _, warning in parsed) or len(candidates) > 1:
            ambiguous.add("validity")
    return DocumentFields(subject, expiry, validity, tuple(dict.fromkeys(warnings)), defaults.decision_number, frozenset(ambiguous))


def merge_fields(results: list[DocumentFields]) -> DocumentFields:
    subjects = {r.subject for r in results if r.subject}
    validities = {(r.validity, r.expiry) for r in results if r.validity}
    numbers = {r.decision_number for r in results if r.decision_number}
    warnings = [warning for result in results for warning in result.warnings]
    ambiguous = set().union(*(r.ambiguous_fields for r in results))
    if len(subjects) > 1:
        warnings.append("A fájlok eltérő tárgyat tartalmaznak; válassza ki kézzel a megfelelőt.")
        ambiguous.add("subject")
    if len(validities) > 1:
        warnings.append("A fájlok eltérő lejáratot tartalmaznak; válassza ki kézzel a megfelelőt.")
        ambiguous.add("validity")
    if len(numbers) > 1:
        warnings.append("A fájlok eltérő határozatszámot tartalmaznak; válassza ki kézzel a megfelelőt.")
        ambiguous.add("decision_number")
    validity, expiry = next(iter(validities)) if len(validities) == 1 and "validity" not in ambiguous else (None, None)
    return DocumentFields(
        next(iter(subjects)) if len(subjects) == 1 and "subject" not in ambiguous else None,
        expiry, validity, tuple(dict.fromkeys(warnings)),
        next(iter(numbers)) if len(numbers) == 1 and "decision_number" not in ambiguous else None,
        frozenset(ambiguous),
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
        texts = []
        warnings = []
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
                        if has_images and (not fields.subject or not fields.validity or not fields.decision_number):
                            scale = min(3, 3500 / max(page.get_size()))
                            try:
                                with closing(page.render(scale=scale)) as bitmap:
                                    with bitmap.to_pil() as image:
                                        scanned_text = ocr(image)
                            except (OSError, RuntimeError) as exc:
                                if not text.strip():
                                    raise
                                warnings.append(f"A kép OCR-felismerése sikertelen: {exc}")
                            else:
                                text = text + "\n\n" + scanned_text
                        texts.append(text)
        else:
            with Image.open(path) as image:
                for frame in ImageSequence.Iterator(image):
                    texts.append(ocr(frame))
        check_cancelled()
        fields = extract_fields("\f".join(texts), rules)
        return DocumentFields(
            fields.subject, fields.expiry, fields.validity,
            tuple(dict.fromkeys((*fields.warnings, *warnings))), fields.decision_number, fields.ambiguous_fields,
        )
    except DocumentReadCancelled:
        raise
    except DocumentReadError:
        raise
    except (OSError, ValueError, RuntimeError, pdfium.PdfiumError, Image.DecompressionBombError) as exc:
        raise DocumentReadError(
            f"A dokumentum nem olvasható: {exc}\n"
            "OCR-hez Tesseract és a magyar (hun) nyelvi csomag szükséges."
        ) from exc
