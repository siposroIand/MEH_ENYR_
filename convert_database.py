#!/usr/bin/env python3
"""Offline conversion: python convert_database.py old.db new.db (standard library only)."""
from __future__ import annotations

import argparse
from contextlib import closing
import logging
import os
from pathlib import Path
import sqlite3
import tempfile

from core.db import PERMIT_COLUMNS, create_schema, lookup_id, validate_schema
from core.permit_details import DETAIL_COLUMNS, LEGACY_DETAIL_COLUMNS, replace_details

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
            version = snapshot.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Ez nem a támogatott régi adatbázis-verzió (0 vagy 1); a kész új adatbázist nem konvertáljuk újra.")
            catalogs = {"hatosagok", "engedelytipusok"}
            allowed = {"telephelyek", "engedelyek", "db_events"} if version == 0 else {"telephelyek", "engedelyek", *catalogs}
            if not {"telephelyek", "engedelyek"} <= tables or tables - allowed or (version == 1 and tables != allowed):
                raise ValueError("Nem támogatott régi adatbázis; ismeretlen vagy hiányzó táblák.")
            site_columns = {r[1] for r in snapshot.execute("PRAGMA table_info(telephelyek)")}
            permit_columns = {r[1] for r in snapshot.execute("PRAGMA table_info(engedelyek)")}
            supported = set(PERMIT_COLUMNS) | LEGACY_DETAIL_COLUMNS
            if version == 0:
                supported = (supported - {"hatosag_id", "engedelytipus_id"}) | {"kategoria", "kiado_hatosag"}
            if not {"id", "nev"} <= site_columns or site_columns - {"id", "nev", "ktj", "cim"}:
                raise ValueError("Nem támogatott telephelyoszlopok; az átalakítás megszakadt.")
            if "id" not in permit_columns or permit_columns - supported or (version == 1 and permit_columns != supported):
                raise ValueError("Nem támogatott engedélyoszlopok; az átalakítás megszakadt.")
            if version == 1:
                for table in catalogs:
                    if {r[1] for r in snapshot.execute(f"PRAGMA table_info({table})")} != {"id", "nev"}:
                        raise ValueError(f"Nem támogatott törzstábla: {table}")
            with tempfile.TemporaryDirectory(prefix=".db-conversion-", dir=destination_path.parent) as temp:
                converted_path = Path(temp) / "converted.db"
                with closing(sqlite3.connect(converted_path)) as target:
                    target.execute("PRAGMA foreign_keys=ON")
                    with target:
                        target.execute("BEGIN")
                        create_schema(target)
                        if version == 1:
                            for table in catalogs:
                                target.executemany(
                                    f"INSERT INTO {table} (id, nev) VALUES (?, ?)",
                                    snapshot.execute(f"SELECT id, nev FROM {table}"),
                                )
                        sites = set()
                        for row in snapshot.execute("SELECT * FROM telephelyek"):
                            record = dict(row)
                            target.execute(
                                "INSERT INTO telephelyek (id, nev, ktj, cim) VALUES (?, ?, ?, ?)",
                                tuple(record.get(k) for k in ("id", "nev", "ktj", "cim")),
                            )
                            sites.add(record["id"])
                        count = 0
                        detail_counts = dict.fromkeys(DETAIL_COLUMNS, 0)
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
                            if version == 0:
                                record["hatosag_id"] = lookup_id(target, "hatosagok", record.pop("kiado_hatosag", None))
                                record["engedelytipus_id"] = lookup_id(target, "engedelytipusok", record.pop("kategoria", None))
                            details = {key: record.pop(key) for key in LEGACY_DETAIL_COLUMNS if key in record}
                            columns = list(record)
                            target.execute(
                                f"INSERT INTO engedelyek ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                                tuple(record.values()),
                            )
                            for table, rows in replace_details(target, record["id"], details).items():
                                detail_counts[table] += rows
                            count += 1
                    validate_schema(target)
                    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchone():
                        raise sqlite3.DatabaseError("Az átalakított adatbázis ellenőrzése sikertelen.")
                    counts = {table: target.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                              for table in ("telephelyek", "engedelyek", "hatosagok", "engedelytipusok", *DETAIL_COLUMNS)}
                    if counts["engedelyek"] != count or counts["telephelyek"] != len(sites):
                        raise sqlite3.DatabaseError("Az átalakított rekordszám eltér.")
                    if any(counts[table] != expected for table, expected in detail_counts.items()):
                        raise sqlite3.DatabaseError("Az átalakított részletrekordok száma eltér.")
                    if version == 1 and any(
                        counts[table] != snapshot.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                        for table in catalogs
                    ):
                        raise sqlite3.DatabaseError("Az átalakított törzsrekordok száma eltér.")
                # Atomic publication without replacing a destination created concurrently.
                os.link(converted_path, destination_path)
            if "db_events" in tables:
                logger.warning("A régi db_events eseménynapló a forrásfájlban marad; az új adatbázis hét üzleti táblát tartalmaz.")
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
