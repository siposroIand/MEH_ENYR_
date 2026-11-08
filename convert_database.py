#!/usr/bin/env python3
"""Offline conversion: python convert_database.py old.db new.db (standard library only)."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import date, timedelta
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile

from core.db import PERMIT_COLUMNS, create_schema, lookup_id, validate_schema

logger = logging.getLogger(__name__)


def _backfill(record: dict) -> None:
    record["deleted"] = record.get("deleted") or 0
    if not record.get("hataly_tipus"):
        if record.get("hatarozatlan") or record.get("lejarat") == "9999-12-31":
            record["hataly_tipus"] = "indefinite"
        elif record.get("lejarat"):
            record["hataly_tipus"] = "fixed"
        else:
            record["hataly_tipus"] = "until_modified"
    reviews = []
    review_date = record.get("felulvizsgalat_date")
    if review_date:
        reviews.append({"type": "date", "date": review_date, "note": ""})
    days, expiry = record.get("felulvizsgalat_nap"), record.get("lejarat")
    if days and expiry:
        try:
            calculated = (date.fromisoformat(expiry) - timedelta(days=int(days))).isoformat()
        except (ValueError, TypeError, OverflowError) as exc:
            logger.warning("Nem számítható felülvizsgálat (%s); az eredeti értékek megmaradnak: %s", record["id"], exc)
        else:
            if not review_date:
                record["felulvizsgalat_date"] = calculated
            if not any(r["date"] == calculated for r in reviews):
                reviews.append({"type": "date", "date": calculated, "note": f"{days} nappal a lejárat előtt (számított)"})
    if reviews and not record.get("felulvizsgalatok"):
        record["felulvizsgalatok"] = json.dumps(reviews, ensure_ascii=False)


def convert_database(source_path: Path, destination_path: Path) -> dict[str, int]:
    source_path, destination_path = Path(source_path).resolve(), Path(destination_path).resolve()
    if source_path == destination_path:
        raise ValueError("A forrás és a cél nem lehet azonos; a régi adatbázis megmarad.")
    if destination_path.exists():
        raise FileExistsError(f"A cél már létezik, nem írjuk felül: {destination_path}")
    with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source:
        # A consistent snapshot also includes committed rows still in the source WAL.
        with closing(sqlite3.connect(":memory:")) as snapshot:
            source.backup(snapshot)
            snapshot.row_factory = sqlite3.Row
            if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("Sérült forrásadatbázis.")
            tables = {r[0] for r in snapshot.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not {"telephelyek", "engedelyek"} <= tables or tables - {"telephelyek", "engedelyek", "db_events"}:
                raise ValueError("Nem támogatott régi adatbázis; ismeretlen vagy hiányzó táblák.")
            if snapshot.execute("PRAGMA user_version").fetchone()[0] != 0:
                raise ValueError("Ez nem a támogatott régi adatbázis-verzió.")
            site_columns = {r[1] for r in snapshot.execute("PRAGMA table_info(telephelyek)")}
            permit_columns = {r[1] for r in snapshot.execute("PRAGMA table_info(engedelyek)")}
            supported = (set(PERMIT_COLUMNS) - {"hatosag_id", "engedelytipus_id"}) | {"kategoria", "kiado_hatosag"}
            if not {"id", "nev"} <= site_columns or site_columns - {"id", "nev", "ktj", "cim"}:
                raise ValueError("Nem támogatott telephelyoszlopok; az átalakítás megszakadt.")
            if "id" not in permit_columns or permit_columns - supported:
                raise ValueError("Nem támogatott engedélyoszlopok; az átalakítás megszakadt.")
            with tempfile.TemporaryDirectory(prefix=".db-conversion-", dir=destination_path.parent) as temp:
                converted_path = Path(temp) / "converted.db"
                with closing(sqlite3.connect(converted_path)) as target:
                    target.execute("PRAGMA foreign_keys=ON")
                    with target:
                        create_schema(target)
                        sites = set()
                        for row in snapshot.execute("SELECT * FROM telephelyek"):
                            record = dict(row)
                            target.execute(
                                "INSERT INTO telephelyek (id, nev, ktj, cim) VALUES (?, ?, ?, ?)",
                                tuple(record.get(k) for k in ("id", "nev", "ktj", "cim")),
                            )
                            sites.add(record["id"])
                        count = 0
                        for row in snapshot.execute("SELECT * FROM engedelyek"):
                            record = dict(row)
                            _backfill(record)
                            site = record.get("telephely_id")
                            if site and site not in sites:
                                if not record["deleted"]:
                                    raise ValueError(f"Hiányzó telephely ({site}), engedély: {record['id']}. Javítsa a forrást.")
                                logger.warning("Törölt engedély hiányzó telephelyhivatkozása kiürítve: %s -> %s", record["id"], site)
                                record["telephely_id"] = None
                            elif site == "":
                                record["telephely_id"] = None
                            record["hatosag_id"] = lookup_id(target, "hatosagok", record.pop("kiado_hatosag", None))
                            record["engedelytipus_id"] = lookup_id(target, "engedelytipusok", record.pop("kategoria", None))
                            columns = list(record)
                            target.execute(
                                f"INSERT INTO engedelyek ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                                tuple(record.values()),
                            )
                            count += 1
                    validate_schema(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchone():
                        raise sqlite3.DatabaseError("Az átalakított adatbázis ellenőrzése sikertelen.")
                    counts = {table: target.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                              for table in ("telephelyek", "engedelyek", "hatosagok", "engedelytipusok")}
                    if counts["engedelyek"] != count or counts["telephelyek"] != len(sites):
                        raise sqlite3.DatabaseError("Az átalakított rekordszám eltér.")
                # Atomic publication without replacing a destination created concurrently.
                os.link(converted_path, destination_path)
            if "db_events" in tables:
                logger.warning("A régi db_events eseménynapló a forrásfájlban marad; az új adatbázis négy üzleti táblát tartalmaz.")
            return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Régi engedélyadatbázis átalakítása új fájlba. Előtte zárja be az alkalmazás minden példányát.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        counts = convert_database(args.source, args.destination)
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.error("Az átalakítás sikertelen: %s", exc)
        return 1
    print(f"Elkészült: {args.destination}")
    print(", ".join(f"{table}: {count}" for table, count in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
