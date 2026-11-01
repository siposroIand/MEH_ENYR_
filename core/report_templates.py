"""Versioned, local report presets. SQLite provides atomic concurrent updates."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from core.report_data import GROUPS, VALIDITY_STATES, parse_date


OPTION_KEYS = {
    "title", "format", "group_by", "per_group", "include_archived",
    "site_filter_enabled", "telephely_ids", "selected_categories",
    "selected_authorities", "text_filter", "validity_state", "columns",
    "column_order", "filename_hint", "include_logo", "date_filter",
}


def validate_options(options):
    if not isinstance(options, dict):
        raise ValueError("A sablon beállításai nem érvényesek.")
    result = {k: v for k, v in options.items() if k in OPTION_KEYS}
    for key in ("title", "text_filter", "filename_hint"):
        if key in result and (not isinstance(result[key], str) or len(result[key]) > 10000):
            raise ValueError(f"Érvénytelen sablonmező: {key}")
    for key in ("per_group", "include_archived", "site_filter_enabled", "include_logo"):
        if key in result and not isinstance(result[key], bool):
            raise ValueError(f"Érvénytelen sablonmező: {key}")
    for key in ("telephely_ids", "selected_categories", "selected_authorities", "columns", "column_order"):
        value = result.get(key)
        if key in ("columns", "column_order") and key in result and value is None:
            raise ValueError(f"Érvénytelen sablonmező: {key}")
        if value is not None and (
            not isinstance(value, list) or len(value) > 10000
            or any(not isinstance(v, (str, int)) or isinstance(v, bool) for v in value)
        ):
            raise ValueError(f"Érvénytelen sablonmező: {key}")
    if result.get("format", "PDF") not in ("PDF", "XLSX"):
        raise ValueError("Ismeretlen sablonformátum.")
    if result.get("group_by") not in dict(GROUPS):
        raise ValueError("Ismeretlen sabloncsoportosítás.")
    if result.get("validity_state", "all") not in dict(VALIDITY_STATES):
        raise ValueError("Ismeretlen érvényességi szűrő.")
    df = result.get("date_filter", {})
    if not isinstance(df, dict) or not isinstance(df.get("enabled", False), bool):
        raise ValueError("Érvénytelen sablondátumszűrő.")
    df = {k: df.get(k) for k in ("enabled", "from", "to")}
    df["enabled"] = bool(df["enabled"])
    for key in ("from", "to"):
        value = df[key]
        if value is not None:
            parsed = parse_date(value)
            if parsed is None:
                raise ValueError("Érvénytelen sablondátum.")
            df[key] = parsed.isoformat()
    if df["enabled"] and df["from"] and df["to"] and df["from"] > df["to"]:
        raise ValueError("A kezdő dátum nem lehet későbbi a záró dátumnál.")
    result["date_filter"] = df
    # Validate serializability before opening the database.
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result


class ReportTemplateStore:
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS report_templates "
                "(name TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            connection.commit()
        except Exception:
            connection.close()
            raise
        return connection

    def names(self):
        with closing(self._connect()) as connection:
            names = [row[0] for row in connection.execute("SELECT name FROM report_templates")]
        return sorted(names, key=str.casefold)

    def save(self, name, options, *, overwrite=False):
        name = name.strip()
        if not name or len(name) > 120 or any(ord(c) < 32 for c in name):
            raise ValueError("A sablonnév 1–120 karakter hosszú, egysoros szöveg legyen.")
        payload = json.dumps({"version": 1, "options": validate_options(options)}, ensure_ascii=False)
        with closing(self._connect()) as connection, connection:
            if overwrite:
                connection.execute(
                    "INSERT INTO report_templates(name, payload) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET payload=excluded.payload", (name, payload)
                )
            else:
                try:
                    connection.execute("INSERT INTO report_templates VALUES (?, ?)", (name, payload))
                except sqlite3.IntegrityError as exc:
                    raise ValueError("Ilyen nevű sablon már létezik.") from exc

    def load(self, name):
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT payload FROM report_templates WHERE name=?", (name,)).fetchone()
        if row is None:
            raise ValueError("A sablon nem található.")
        payload = json.loads(row[0])
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Nem támogatott sablonverzió.")
        return validate_options(payload.get("options"))

    def delete(self, name):
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute("DELETE FROM report_templates WHERE name=?", (name,))
            if cursor.rowcount != 1:
                raise ValueError("A sablon nem található.")
