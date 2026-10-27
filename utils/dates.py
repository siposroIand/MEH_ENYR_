from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any


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