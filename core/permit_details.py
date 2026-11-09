"""Relational permit details and the shared GUI/export compatibility mapping."""
from datetime import date
import json
import sqlite3


WASTE_FIELDS = (
    "hulladek_nv_elokez", "hulladek_v_elokez",
    "hulladek_nv_gyujt", "hulladek_v_gyujt",
)
LEGACY_DETAIL_COLUMNS = {
    "modosito_hat", "felulvizsgalatok", "felulvizsgalat_date",
    "felulvizsgalat_nap", *WASTE_FIELDS,
}
DETAIL_COLUMNS = {
    "felulvizsgalatok": {"engedely_id", "sorrend", "tipus", "datum", "napok", "megjegyzes"},
    "modosito_hatarozatok": {"engedely_id", "sorrend", "hatarozatszam"},
    "hulladekadatok": {"engedely_id", "tipus", "mennyiseg", "egyseg"},
}


def create_detail_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE felulvizsgalatok (
            engedely_id TEXT NOT NULL REFERENCES engedelyek(id) ON DELETE CASCADE,
            sorrend INTEGER NOT NULL CHECK(sorrend >= 0),
            tipus TEXT NOT NULL CHECK(tipus IN ('date', 'before_expiry')),
            datum TEXT,
            napok INTEGER,
            megjegyzes TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (engedely_id, sorrend),
            CHECK ((tipus='date' AND datum IS NOT NULL AND napok IS NULL)
                OR (tipus='before_expiry' AND datum IS NULL AND napok > 0 AND napok IS NOT NULL))
        )
    """)
    conn.execute("""
        CREATE TABLE modosito_hatarozatok (
            engedely_id TEXT NOT NULL REFERENCES engedelyek(id) ON DELETE CASCADE,
            sorrend INTEGER NOT NULL CHECK(sorrend >= 0),
            hatarozatszam TEXT NOT NULL,
            PRIMARY KEY (engedely_id, sorrend)
        )
    """)
    codes = ", ".join(f"'{field}'" for field in WASTE_FIELDS)
    conn.execute(f"""
        CREATE TABLE hulladekadatok (
            engedely_id TEXT NOT NULL REFERENCES engedelyek(id) ON DELETE CASCADE,
            tipus TEXT NOT NULL CHECK(tipus IN ({codes})),
            mennyiseg TEXT NOT NULL,
            egyseg TEXT CHECK(egyseg IN ('t', 'kg')),
            PRIMARY KEY (engedely_id, tipus)
        )
    """)


def replace_details(conn: sqlite3.Connection, entity_id: str, data: dict) -> dict[str, int]:
    """Replace all children inside the caller's permit transaction."""
    try:
        reviews = data.get("felulvizsgalatok")
        if isinstance(reviews, str):
            if reviews.strip():
                reviews = json.loads(reviews)
                if not isinstance(reviews, list):
                    raise ValueError("A felülvizsgálati JSON-nak listát kell tartalmaznia.")
            else:
                reviews = None
        if reviews is None:
            reviews = []
            if data.get("felulvizsgalat_date"):
                reviews.append({"type": "date", "date": data["felulvizsgalat_date"]})
            if data.get("felulvizsgalat_nap"):
                days = data["felulvizsgalat_nap"]
                if isinstance(days, str):
                    days = int(days)
                reviews.append({"type": "before_expiry", "days": days})
        if not isinstance(reviews, list):
            raise ValueError("A felülvizsgálatoknak listát kell alkotniuk.")
        for table in DETAIL_COLUMNS:
            conn.execute(f"DELETE FROM {table} WHERE engedely_id=?", (entity_id,))
        for position, review in enumerate(reviews):
            if not isinstance(review, dict) or review.keys() - {"type", "date", "days", "note"}:
                raise ValueError("Ismeretlen felülvizsgálati adatszerkezet.")
            kind = review.get("type") or ("date" if review.get("date") else None)
            day, days = review.get("date"), review.get("days")
            if kind == "date":
                if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day or days is not None:
                    raise ValueError("Hibás felülvizsgálati dátum.")
            elif kind == "before_expiry":
                if type(days) is not int or days <= 0 or day is not None:
                    raise ValueError("Hibás felülvizsgálati napérték.")
            else:
                raise ValueError("Ismeretlen felülvizsgálati típus.")
            note = review.get("note", "")
            if not isinstance(note, str):
                raise ValueError("A felülvizsgálati megjegyzés nem szöveg.")
            conn.execute(
                "INSERT INTO felulvizsgalatok VALUES (?, ?, ?, ?, ?, ?)",
                (entity_id, position, kind, day, days, note),
            )
        amendments = data.get("modositok")
        if amendments is None:
            raw = data.get("modosito_hat")
            amendments = raw.split(";") if raw else []
        if not isinstance(amendments, list) or not all(isinstance(item, str) for item in amendments):
            raise ValueError("A módosító határozatoknak szöveges listát kell alkotniuk.")
        conn.executemany(
            "INSERT INTO modosito_hatarozatok VALUES (?, ?, ?)",
            ((entity_id, position, value) for position, value in enumerate(amendments)),
        )
        waste_count = 0
        for field in WASTE_FIELDS:
            value = data.get(field)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError("A hulladékmennyiség nem szöveg.")
            amount, separator, unit = value.rpartition(" ")
            if not separator or unit not in ("t", "kg"):
                amount, unit = value, None
            conn.execute("INSERT INTO hulladekadatok VALUES (?, ?, ?, ?)", (entity_id, field, amount, unit))
            waste_count += 1
        return {
            "felulvizsgalatok": len(reviews), "modosito_hatarozatok": len(amendments),
            "hulladekadatok": waste_count,
        }
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise sqlite3.IntegrityError(f"Hibás engedélyrészletek ({entity_id}): {exc}") from exc


def load_details(conn: sqlite3.Connection, permits: list[dict]) -> list[dict]:
    """Batch reads share the parent query's snapshot; exports retain their format."""
    for offset in range(0, len(permits), 400):
        batch = {permit["id"]: permit for permit in permits[offset:offset + 400]}
        reviews = {identifier: [] for identifier in batch}
        for permit in batch.values():
            permit.update({field: None for field in LEGACY_DETAIL_COLUMNS})
            permit["modositok"] = []
        placeholders = ", ".join("?" for _ in batch)
        for row in conn.execute(
            f"SELECT * FROM felulvizsgalatok WHERE engedely_id IN ({placeholders}) ORDER BY sorrend", tuple(batch),
        ):
            item = {"type": row["tipus"], "note": row["megjegyzes"]}
            item["date" if row["tipus"] == "date" else "days"] = row["datum"] if row["tipus"] == "date" else row["napok"]
            reviews[row["engedely_id"]].append(item)
        for row in conn.execute(
            f"SELECT * FROM modosito_hatarozatok WHERE engedely_id IN ({placeholders}) ORDER BY sorrend", tuple(batch),
        ):
            batch[row["engedely_id"]]["modositok"].append(row["hatarozatszam"])
        for row in conn.execute(f"SELECT * FROM hulladekadatok WHERE engedely_id IN ({placeholders})", tuple(batch)):
            batch[row["engedely_id"]][row["tipus"]] = row["mennyiseg"] + (
                f" {row['egyseg']}" if row["egyseg"] is not None else ""
            )
        for identifier, permit in batch.items():
            items = reviews[identifier]
            permit["felulvizsgalatok"] = json.dumps(items, ensure_ascii=False)
            dates = [item["date"] for item in items if item["type"] == "date"]
            days = [item["days"] for item in items if item["type"] == "before_expiry"]
            permit["felulvizsgalat_date"] = dates[0] if len(dates) == 1 else None
            permit["felulvizsgalat_nap"] = days[0] if len(days) == 1 else None
            permit["modosito_hat"] = ";".join(permit["modositok"]) if permit["modositok"] else None
    return permits
