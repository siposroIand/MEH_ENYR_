"""Lossless review parsing and shared, Qt-independent date calculations."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, TypedDict


class ReviewEntry(TypedDict, total=False):
    type: str
    date: str
    days: int
    note: str


class ReviewValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewParseResult:
    entries: tuple[ReviewEntry, ...]
    errors: tuple[str, ...] = ()
    raw: Any = None
    uses_legacy: bool = False

    @property
    def valid(self) -> bool:
        return not self.errors


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            try:
                return datetime.strptime(value, "%Y.%m.%d").date()
            except ValueError:
                return None


def parse_date(value: Any) -> date | None:
    """Parse ISO dates/timestamps and dotted legacy dates without raising."""
    return _date(value)


def _days(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("A napok száma nemnegatív egész szám legyen.")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise ValueError("A napok száma nemnegatív egész szám legyen.")
    if not 0 <= result < date.max.toordinal():
        raise ValueError("A napok száma kívül esik a támogatott naptári tartományon.")
    return result


def parse_reviews(entity: Mapping[str, Any]) -> ReviewParseResult:
    """Parse typed or legacy entries, never replacing an explicit empty list.

    Untyped entries with an explicit ``days`` key are relative, even when they
    also contain a previously calculated ``date``. The original source remains
    available as ``raw``. Invalid input must not be replaced by only ``entries``.
    """
    raw = entity.get("felulvizsgalatok")
    legacy = raw is None or raw == ""
    errors: list[str] = []
    entries: list[ReviewEntry] = []
    if legacy:
        source = []
        explicit = entity.get("felulvizsgalat_date")
        if explicit not in (None, ""):
            source.append({"type": "date", "date": explicit})
        days = entity.get("felulvizsgalat_nap")
        if days not in (None, "", 0, "0"):
            source.append({"type": "before_expiry", "days": days})
    else:
        source = raw
        if isinstance(source, str):
            try:
                source = json.loads(source)
            except (ValueError, TypeError):
                return ReviewParseResult((), ("A felülvizsgálati adatok JSON-formátuma hibás.",), raw)
        if not isinstance(source, list):
            return ReviewParseResult((), ("A felülvizsgálati adatoknak listát kell alkotniuk.",), raw)
    for index, item in enumerate(source):
        try:
            if not isinstance(item, dict):
                raise ValueError("A bejegyzésnek adatobjektumnak kell lennie.")
            kind = item.get("type")
            if kind is None:
                if "days" in item:
                    kind = "before_expiry"
                elif "date" in item:
                    kind = "date"
            note = item.get("note", "")
            if note is None:
                note = ""
            if not isinstance(note, str):
                raise ValueError("A megjegyzés csak szöveg lehet.")
            if kind == "date":
                actual = _date(item.get("date"))
                if actual is None:
                    raise ValueError("Érvénytelen felülvizsgálati dátum.")
                entries.append({"type": "date", "date": actual.isoformat(), "note": note})
            elif kind == "before_expiry":
                days = _days(item.get("days"))
                expiry = _date(entity.get("lejarat"))
                if (expiry and entity.get("hataly_tipus") not in ("indefinite", "until_modified")
                        and days >= expiry.toordinal()):
                    raise ValueError("A lejárat előtti dátum kívül esik a támogatott naptári tartományon.")
                entries.append({"type": kind, "days": days, "note": note})
            else:
                raise ValueError("Ismeretlen felülvizsgálati típus.")
        except ValueError as exc:
            errors.append(f"{index + 1}. felülvizsgálati bejegyzés: {exc}")
    return ReviewParseResult(tuple(entries), tuple(errors), raw, legacy)


def review_occurrences(entity: Mapping[str, Any]) -> list[tuple[date, ReviewEntry]]:
    """Resolve each calculable entry while retaining its type and note."""
    result = parse_reviews(entity)
    expiry = _date(entity.get("lejarat"))
    if (entity.get("hataly_tipus") in ("indefinite", "until_modified")
            or str(entity.get("hatarozatlan") or "0") not in ("0", "False")
            or expiry == date.max):
        expiry = None
    occurrences: list[tuple[date, ReviewEntry]] = []
    for entry in result.entries:
        if entry["type"] == "date":
            occurrences.append((date.fromisoformat(entry["date"]), entry))
        elif expiry is not None:
            try:
                occurrences.append((expiry - timedelta(days=entry["days"]), entry))
            except OverflowError:
                continue
    return sorted(occurrences, key=lambda occurrence: occurrence[0])


def review_dates(entity: Mapping[str, Any]) -> list[date]:
    return [due for due, _ in review_entries(entity)]


def review_entries(entity: Mapping[str, Any]) -> list[tuple[date, str]]:
    """Return resolved dates and their original notes, without deduplication."""
    return [(due, entry.get("note", "")) for due, entry in review_occurrences(entity)]


def next_review_date(entity: Mapping[str, Any], today: date | None = None) -> date | None:
    today = today or date.today()
    return next((value for value in review_dates(entity) if value >= today), None)


def compute_review_date(entity: Mapping[str, Any]) -> date | None:
    dates = review_dates(entity)
    return dates[0] if dates else None
