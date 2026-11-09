from __future__ import annotations

from datetime import datetime, date, timedelta
import json
from typing import Optional, Dict, Any


def extract_review_entries(entity: Dict[str, Any]) -> list[tuple[date, str]]:
    """Resolve every typed review using the current expiry, for UI and exports."""
    raw = entity.get("felulvizsgalatok")
    if raw is not None and raw != "":
        reviews = json.loads(raw) if isinstance(raw, str) else raw
    else:
        reviews = []
        if entity.get("felulvizsgalat_date"):
            reviews.append({"type": "date", "date": entity["felulvizsgalat_date"]})
        if entity.get("felulvizsgalat_nap"):
            reviews.append({"type": "before_expiry", "days": int(entity["felulvizsgalat_nap"])})
    if not isinstance(reviews, list):
        raise ValueError("A felülvizsgálatoknak listát kell alkotniuk.")

    def parse(value: str) -> date:
        parsed = parse_iso_date(value)
        return parsed if parsed is not None else datetime.strptime(value, "%Y.%m.%d").date()

    expiry = None
    if entity.get("lejarat") and entity.get("hataly_tipus") not in ("indefinite", "until_modified") \
            and not entity.get("hatarozatlan") and not is_indefinite_lejarat(entity["lejarat"]):
        expiry = parse(entity["lejarat"])
    result: dict[date, list[str]] = {}
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("Hibás felülvizsgálati bejegyzés.")
        kind = review.get("type") or ("date" if review.get("date") else None)
        note = review.get("note") or ""
        if not isinstance(note, str):
            raise ValueError("A felülvizsgálati megjegyzés nem szöveg.")
        if kind == "date":
            day = parse(review["date"])
        elif kind == "before_expiry":
            days = review.get("days")
            if type(days) is not int or days <= 0:
                raise ValueError("Hibás felülvizsgálati napérték.")
            if expiry is None:
                continue
            day = expiry - timedelta(days=days)
            note = note or f"{days} nappal a lejárat előtt (számított)"
        else:
            raise ValueError("Ismeretlen felülvizsgálati típus.")
        notes = result.setdefault(day, [])
        if note and note not in notes:
            notes.append(note)
    return [(day, "; ".join(notes)) for day, notes in sorted(result.items())]


def parse_iso_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        try:
            return datetime.fromisoformat(s).date()
        except Exception:
            return None


def is_indefinite_lejarat(lejarat_str: Optional[str]) -> bool:
    if not lejarat_str:
        return False
    return lejarat_str.strip() == "9999-12-31"


def compute_review_date(entity: Dict[str, Any]) -> Optional[date]:
    fv_str = entity.get("felulvizsgalat_date")
    fv = parse_iso_date(fv_str)
    if fv:
        return fv

    try:
        fv_nap = int(entity.get("felulvizsgalat_nap") or 0)
    except Exception:
        fv_nap = 0

    hataly_tipus = (entity.get("hataly_tipus") or "").strip().lower()
    hatarozatlan_flag = bool(
        int(entity.get("hatarozatlan") or 0)
        if entity.get("hatarozatlan") is not None
        else False
    )

    lejarat_raw = entity.get("lejarat")
    lejarat = parse_iso_date(lejarat_raw)
    if lejarat and is_indefinite_lejarat(lejarat_raw):
        lejarat = None

    if fv_nap > 0 and lejarat:
        try:
            return lejarat - timedelta(days=fv_nap)
        except Exception:
            return None

    if hatarozatlan_flag or hataly_tipus == "indefinite":
        return None

    return None