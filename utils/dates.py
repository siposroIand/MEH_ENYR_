from __future__ import annotations

from datetime import datetime, date
from typing import Optional, Dict, Any

from utils.reviews import compute_review_date as _compute_review_date


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
    """Compatibility entry point for the shared typed/legacy review parser."""
    return _compute_review_date(entity)
