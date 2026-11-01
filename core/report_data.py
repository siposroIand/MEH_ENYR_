"""Report filtering and grouping, independent of widgets and output formats."""
from datetime import date, datetime


VALIDITY_STATES = [
    ("all", "Mindegyik"),
    ("active", "Érvényes (nem lejárt)"),
    ("expired", "Lejárt"),
    ("expiring", "30 napon belül lejár"),
    ("fixed", "Határozott idejű"),
    ("indefinite", "Határozatlan"),
    ("until_change", "Változtatásig"),
    ("unknown", "Ismeretlen lejárat"),
]
GROUPS = [
    (None, "Nincs (Összes)"),
    ("telephely", "Telephelyenként"),
    ("kategoria", "Engedélytípusonként"),
    ("kiado_hatosag", "Kiadó hatóság szerint"),
    ("lejarat_month", "Lejárat szerint (Év-Hónap)"),
    ("lejarat_year", "Lejárat szerint (Év)"),
    ("status", "Érvényességi állapot szerint"),
]
UNKNOWN = "Ismeretlen"


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return datetime.strptime(value, "%Y.%m.%d").date()
        except ValueError:
            return None


def is_flag(value):
    return value is True or value == 1 or str(value).lower() in ("1", "true")


def validity_type(item):
    kind = str(item.get("hataly_tipus") or "").strip().lower()
    if kind == "until_modified":
        return "until_change"
    if kind:
        return kind
    if is_flag(item.get("hatarozatlan")):
        return "indefinite"
    return "fixed" if item.get("lejarat") else "until_change"


def validity_state(item, today=None):
    today = today or date.today()
    kind = validity_type(item)
    if kind in ("indefinite", "until_change"):
        return "active"
    expiry = parse_date(item.get("lejarat"))
    if kind != "fixed" or expiry is None:
        return "unknown"
    return "expired" if expiry < today else "active"


def build_report_groups(items, telephelyek, options, today=None):
    """Apply all filters once; retain stable input order within sorted groups.

    Absent/None selection means all; an explicit empty selection means none.
    Legacy empty telephely_ids still means all unless site_filter_enabled is set.
    """
    today = today or date.today()
    df = options.get("date_filter") or {}
    start, end = parse_date(df.get("from")), parse_date(df.get("to"))
    if df.get("enabled"):
        if (df.get("from") and start is None) or (df.get("to") and end is None):
            raise ValueError("Érvénytelen dátumszűrő.")
        if start and end and start > end:
            raise ValueError("A kezdő dátum nem lehet későbbi a záró dátumnál.")
    state = options.get("validity_state", "all")
    if state not in dict(VALIDITY_STATES):
        raise ValueError("Ismeretlen érvényességi szűrő.")
    group_by = options.get("group_by")
    if group_by not in dict(GROUPS):
        raise ValueError("Ismeretlen csoportosítás.")
    tp_map = {t["id"]: t.get("nev") or UNKNOWN for t in telephelyek}
    sites = options.get("telephely_ids")
    categories = options.get("selected_categories")
    authorities = options.get("selected_authorities")
    text = str(options.get("text_filter") or "").strip().casefold()
    groups = {}
    for item in items:
        if is_flag(item.get("deleted")):
            continue
        if not options.get("include_archived") and (
            is_flag(item.get("archivalt")) or is_flag(item.get("archived"))
        ):
            continue
        if (sites or options.get("site_filter_enabled")) and item.get("telephely_id") not in (sites or []):
            continue
        category = item.get("kategoria") or UNKNOWN
        authority = item.get("kiado_hatosag") or UNKNOWN
        if categories is not None and category not in categories:
            continue
        if authorities is not None and authority not in authorities:
            continue
        if text:
            values = [tp_map.get(item.get("telephely_id"), UNKNOWN)]
            values.extend(item.get(key) or "" for key in (
                "engedely_nev", "kategoria", "kiado_hatosag", "hatarozatszam",
                "modosito_hat", "megjegyzes", "hatarozatlan_szoveg",
            ))
            if text not in "\n".join(str(v) for v in values).casefold():
                continue
        kind = validity_type(item)
        expiry = parse_date(item.get("lejarat")) if kind == "fixed" else None
        actual_state = validity_state(item, today)
        if state in ("active", "expired", "unknown") and state != actual_state:
            continue
        if state in ("fixed", "indefinite", "until_change") and kind != state:
            continue
        if state == "expiring" and (expiry is None or not 0 <= (expiry - today).days <= 30):
            continue
        if df.get("enabled") and (
            expiry is None or (start and expiry < start) or (end and expiry > end)
        ):
            continue
        group = "Összes"
        if group_by == "telephely":
            group = tp_map.get(item.get("telephely_id"), UNKNOWN)
        elif group_by == "kategoria":
            group = category
        elif group_by == "kiado_hatosag":
            group = authority
        elif group_by in ("lejarat_month", "lejarat_year"):
            group = expiry.strftime("%Y-%m" if group_by == "lejarat_month" else "%Y") if expiry else UNKNOWN
        elif group_by == "status":
            group = {"active": "Érvényes", "expired": "Lejárt", "unknown": UNKNOWN}[actual_state]
        groups.setdefault(group, []).append(item)
    return dict(sorted(groups.items(), key=lambda pair: pair[0].casefold()))
