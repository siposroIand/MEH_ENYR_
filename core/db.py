#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
import time
import functools
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import os
import logging
from random import uniform
import json
from uuid import uuid4

from utils.fs_utils import create_lockfile, release_lockfile

logger = logging.getLogger(__name__)

MAX_RETRIES = 8
RETRY_INTERVAL = 0.15  # base interval in seconds

PERMIT_COLUMNS = (
    "telephely_id", "engedely_nev", "kategoria", "kiado_hatosag",
    "hatarozatszam", "modosito_hat", "letrejott", "hatarozatlan",
    "lejarat", "hataly_tipus", "felulvizsgalat_nap", "felulvizsgalat_date",
    "felulvizsgalatok", "megjegyzes", "hulladek_nv_elokez",
    "hulladek_v_elokez", "hulladek_nv_gyujt", "hulladek_v_gyujt",
)


class ConflictError(sqlite3.IntegrityError):
    """The editor's snapshot no longer matches the stored record."""


def _require_updated(cursor) -> None:
    if cursor.rowcount != 1:
        raise ConflictError(
            "A tételt időközben más módosította vagy törölte. "
            "A beírt adatok megmaradtak, de nem írtuk felül a másik felhasználó munkáját. "
            "Másolja ki a szükséges módosításokat, majd zárja be és nyissa meg újra az adatlapot."
        )


def retry_db(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as e:
                if not any(reason in str(e).lower() for reason in ("locked", "busy")):
                    raise
                last_exc = e
                if attempt == MAX_RETRIES:
                    break
                backoff = RETRY_INTERVAL * (2 ** (attempt - 1))
                jitter = uniform(0, backoff * 0.1)
                sleep_time = backoff + jitter
                logger.debug(
                    "SQLite OperationalError, attempt %d/%d: %s (sleep %.3fs)",
                    attempt, MAX_RETRIES, e, sleep_time
                )
                time.sleep(sleep_time)
            except sqlite3.DatabaseError:
                raise
        logger.error("Retry limit reached, raising last exception")
        raise last_exc
    return wrapper


class Database:
    def __init__(self, db_path: Path, backup_dir: Optional[Path] = None):
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.db_path.parent
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # startup backup (nem kritikus)
        try:
            self.make_backup(reason="startup")
        except Exception as e:
            logger.debug("Nem sikerült startup backup-ot készíteni: %s", e)

        # init & migrate
        self.init_db()
        self.migrate_db()

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=60000;")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @retry_db
    def get_data_version(self) -> int:
        try:
            with self.get_connection() as conn:
                cur = conn.execute("PRAGMA data_version")
                row = cur.fetchone()
                if row:
                    return int(row[0])
        except Exception as e:
            logger.debug("get_data_version failed: %s", e)
        return 0

    @retry_db
    def init_db(self) -> None:
        with self.get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS telephelyek (
                    id TEXT PRIMARY KEY,
                    nev TEXT NOT NULL,
                    ktj TEXT,
                    cim TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS engedelyek (
                    id TEXT PRIMARY KEY,
                    telephely_id TEXT,
                    engedely_nev TEXT,
                    kategoria TEXT,
                    kiado_hatosag TEXT,
                    hatarozatszam TEXT,
                    modosito_hat TEXT,
                    letrejott TEXT,
                    hatarozatlan INTEGER DEFAULT 0,
                    hatarozatlan_szoveg TEXT,
                    lejarat TEXT,
                    felulvizsgalat_nap INTEGER,
                    felulvizsgalat_date TEXT,
                    felulvizsgalatok TEXT, -- JSON lista: [{"date":"YYYY-MM-DD","note":"..."}, ...]
                    megjegyzes TEXT,
                    archivalt INTEGER DEFAULT 0,
                    hulladek_nv_elokez TEXT,
                    hulladek_v_elokez TEXT,
                    hulladek_nv_gyujt TEXT,
                    hulladek_v_gyujt TEXT,
                    last_modified TEXT,
                    deleted INTEGER DEFAULT 0,
                    hataly_tipus TEXT DEFAULT 'fixed',
                    FOREIGN KEY (telephely_id) REFERENCES telephelyek(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS db_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    note TEXT
                )
            """)

    @retry_db
    def migrate_db(self) -> None:
        """
        Egyszerű migráció és backfill:
         - új oszlopok hozzáadása ha szükséges
         - hataly_tipus feltöltése meglévő adatokból
         - felulvizsgalat_date backfill és felulvizsgalatok JSON backfill
        """
        telephely_new_cols = [("ktj", "TEXT"), ("cim", "TEXT")]
        engedely_new_cols = [
            ("hulladek_nv_elokez", "TEXT"),
            ("hulladek_v_elokez", "TEXT"),
            ("hulladek_nv_gyujt", "TEXT"),
            ("hulladek_v_gyujt", "TEXT"),
            ("last_modified", "TEXT"),
            ("deleted", "INTEGER DEFAULT 0"),
            ("hatarozatlan_szoveg", "TEXT"),
            ("hataly_tipus", "TEXT DEFAULT 'fixed'"),
            ("felulvizsgalat_date", "TEXT"),
            ("felulvizsgalatok", "TEXT")
        ]
        with self.get_connection() as conn:
            # Concurrency tokens are required, so migration failures must abort startup.
            for table in ("telephelyek", "engedelyek"):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if "revision" not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN revision TEXT NOT NULL DEFAULT ''")
            cur = conn.cursor()
            added_validity_column = False
            # telephelyek oszlopok
            try:
                cur.execute("PRAGMA table_info(telephelyek)")
                existing = [r[1] for r in cur.fetchall()]
                for name, ctype in telephely_new_cols:
                    if name not in existing:
                        try:
                            cur.execute(f"ALTER TABLE telephelyek ADD COLUMN {name} {ctype}")
                        except Exception as e:
                            logger.debug("Nem sikerült hozzáadni telephelyek oszlopot %s: %s", name, e)
            except Exception as e:
                logger.debug("telephelyek PRAGMA hiba: %s", e)

            # engedelyek oszlopok
            try:
                cur.execute("PRAGMA table_info(engedelyek)")
                existing = [r[1] for r in cur.fetchall()]
                for name, ctype in engedely_new_cols:
                    if name not in existing:
                        try:
                            cur.execute(f"ALTER TABLE engedelyek ADD COLUMN {name} {ctype}")
                            if name == "hataly_tipus":
                                added_validity_column = True
                        except Exception as e:
                            logger.debug("Nem sikerült hozzáadni engedelyek oszlopot %s: %s", name, e)
            except Exception as e:
                logger.debug("engedelyek PRAGMA hiba: %s", e)

            # deleted backfill
            try:
                cur.execute("UPDATE engedelyek SET deleted=0 WHERE deleted IS NULL")
                conn.commit()
            except Exception:
                logger.debug("deleted backfill sikertelen (valószínűleg nincs ilyen oszlop még)")

            # hataly_tipus feltöltése
            try:
                query = "SELECT id, hatarozatlan, lejarat FROM engedelyek"
                if not added_validity_column:
                    query += " WHERE hataly_tipus IS NULL OR hataly_tipus=''"
                cur.execute(query)
                rows = [dict(row) for row in cur.fetchall()]
                updates = []
                for r in rows:
                    rid = r["id"]
                    hatarozatlan = r.get("hatarozatlan")
                    lejarat = r.get("lejarat")
                    new_ht = "until_modified"
                    try:
                        if hatarozatlan is not None and int(hatarozatlan) == 1:
                            new_ht = "indefinite"
                        elif lejarat and str(lejarat).strip() and str(lejarat).strip() != "9999-12-31":
                            new_ht = "fixed"
                        elif lejarat and str(lejarat).strip() == "9999-12-31":
                            new_ht = "indefinite"
                    except Exception:
                        new_ht = "until_modified"
                    updates.append((new_ht, rid))
                if updates:
                    cur.executemany("UPDATE engedelyek SET hataly_tipus=? WHERE id=?", updates)
                    conn.commit()
            except Exception as e:
                logger.debug("Hiba hataly_tipus feltöltés közben: %s", e)

            # Backfill felulvizsgalat_date where possible
            try:
                cur.execute("SELECT id, lejarat, felulvizsgalat_nap, felulvizsgalat_date FROM engedelyek")
                rows = [dict(row) for row in cur.fetchall()]
                updates = []
                for r in rows:
                    rid = r["id"]
                    fv_date_existing = r.get("felulvizsgalat_date")
                    fv_nap = r.get("felulvizsgalat_nap") or 0
                    lejarat = r.get("lejarat")
                    if (not fv_date_existing or str(fv_date_existing).strip() == "") and fv_nap and lejarat:
                        try:
                            ld = datetime.strptime(lejarat, "%Y-%m-%d").date()
                            new_fv = (ld - timedelta(days=int(fv_nap))).isoformat()
                            updates.append((new_fv, rid))
                        except Exception:
                            pass
                if updates:
                    cur.executemany("UPDATE engedelyek SET felulvizsgalat_date=? WHERE id=?", updates)
                    conn.commit()
            except Exception as e:
                logger.debug("Hiba felulvizsgalat_date backfill közben: %s", e)

            # Backfill felulvizsgalatok JSON oszlop a meglévő adatok alapján (csak ha üres)
            try:
                cur.execute("SELECT id, lejarat, felulvizsgalat_nap, felulvizsgalat_date, felulvizsgalatok FROM engedelyek")
                rows = [dict(row) for row in cur.fetchall()]
                updates = []
                for r in rows:
                    rid = r["id"]
                    current_json = r.get("felulvizsgalatok")
                    if current_json and str(current_json).strip():
                        continue
                    entries = []
                    fv_date_existing = r.get("felulvizsgalat_date")
                    if fv_date_existing and str(fv_date_existing).strip():
                        try:
                            # validate format
                            _ = datetime.strptime(fv_date_existing, "%Y-%m-%d")
                            entries.append({"type": "date", "date": fv_date_existing, "note": ""})
                        except Exception:
                            pass
                    fv_nap = r.get("felulvizsgalat_nap") or 0
                    lejarat = r.get("lejarat")
                    if fv_nap and lejarat:
                        try:
                            ld = datetime.strptime(lejarat, "%Y-%m-%d").date()
                            comp = (ld - timedelta(days=int(fv_nap))).isoformat()
                            if not any(e["date"] == comp for e in entries):
                                entries.append({"type": "date", "date": comp, "note": f"{int(fv_nap)} nappal a lejárat előtt (számított)"})
                        except Exception:
                            pass
                    if entries:
                        json_s = json.dumps(entries, ensure_ascii=False)
                        updates.append((json_s, rid))
                if updates:
                    cur.executemany("UPDATE engedelyek SET felulvizsgalatok=? WHERE id=?", updates)
                    conn.commit()
            except Exception as e:
                logger.debug("Hiba felulvizsgalatok backfill közben: %s", e)

    # ---------------- FETCH / EVENTS ----------------
    @retry_db
    def get_all_data(self) -> Dict[str, List[Dict[str, Any]]]:
        with self.get_connection() as conn:
            conn.execute("BEGIN")
            tps = [dict(row) for row in conn.execute("SELECT * FROM telephelyek ORDER BY nev").fetchall()]
            engs = [dict(row) for row in conn.execute("SELECT * FROM engedelyek WHERE (deleted IS NULL OR deleted=0)").fetchall()]
            return {"telephelyek": tps, "engedelyek": engs}

    @retry_db
    def get_telephelyek(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM telephelyek").fetchall()]

    @retry_db
    def save_permit(self, data: dict, entity_id: str, updating: bool = False) -> None:
        if not data.get("telephely_id") or not data.get("hatarozatszam"):
            raise ValueError("A telephely és a határozatszám kötelező.")
        values = dict(data)
        values["felulvizsgalatok"] = json.dumps(data.get("felulvizsgalatok", []), ensure_ascii=False)
        columns = (*PERMIT_COLUMNS, "last_modified", "revision")
        parameters = [values.get(column) for column in PERMIT_COLUMNS]
        parameters.extend((datetime.utcnow().isoformat(), str(uuid4())))
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM telephelyek WHERE id=?", (data["telephely_id"],)).fetchone():
                raise sqlite3.IntegrityError("A kiválasztott telephely már nem létezik.")
            if updating:
                assignments = ", ".join(f"{column}=?" for column in columns)
                cursor = conn.execute(
                    f"UPDATE engedelyek SET {assignments} "
                    "WHERE id=? AND revision=? AND COALESCE(deleted, 0)=0",
                    (*parameters, entity_id, data.get("revision", "")),
                )
                _require_updated(cursor)
            else:
                names = ", ".join(("id", *columns))
                placeholders = ", ".join("?" for _ in range(len(columns) + 1))
                conn.execute(f"INSERT INTO engedelyek ({names}) VALUES ({placeholders})",
                             (entity_id, *parameters))

    @retry_db
    def change_permits(self, records: list[dict], field: str, value: int) -> None:
        if field not in ("archivalt", "deleted") or type(value) is not int or value not in (0, 1):
            raise ValueError("Érvénytelen állapotmódosítás.")
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for record in records:
                cursor = conn.execute(
                    f"UPDATE engedelyek SET {field}=?, last_modified=?, revision=? "
                    "WHERE id=? AND revision=? AND COALESCE(deleted, 0)=0",
                    (value, datetime.utcnow().isoformat(), str(uuid4()),
                     record["id"], record.get("revision", "")),
                )
                _require_updated(cursor)

    @retry_db
    def save_telephely(self, data: dict, entity_id: str, updating: bool = False) -> None:
        if not isinstance(data.get("nev"), str) or not data["nev"].strip():
            raise ValueError("A telephely neve kötelező.")
        values = (data["nev"], data.get("ktj"), data.get("cim"), str(uuid4()))
        with self.get_connection() as conn:
            if updating:
                cursor = conn.execute(
                    "UPDATE telephelyek SET nev=?, ktj=?, cim=?, revision=? WHERE id=? AND revision=?",
                    (*values, entity_id, data.get("revision", "")),
                )
                _require_updated(cursor)
            else:
                conn.execute("INSERT INTO telephelyek (nev, ktj, cim, revision, id) VALUES (?,?,?,?,?)",
                             (*values, entity_id))

    @retry_db
    def delete_telephely(self, entity_id: str, revision: str) -> None:
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM engedelyek WHERE telephely_id=? AND COALESCE(deleted, 0)=0 LIMIT 1",
                (entity_id,),
            ).fetchone():
                raise sqlite3.IntegrityError(
                    "A telephelyhez engedélyek tartoznak; előbb törölje vagy helyezze át azokat."
                )
            _require_updated(conn.execute(
                "DELETE FROM telephelyek WHERE id=? AND revision=?", (entity_id, revision),
            ))

    @retry_db
    def get_changes(self, since: Optional[str]) -> List[Dict[str, Any]]:
        if not since:
            return self.get_all_data().get("engedelyek", [])
        try:
            with self.get_connection() as conn:
                cur = conn.execute("SELECT * FROM engedelyek WHERE last_modified > ? AND (deleted IS NULL OR deleted=0)", (since,))
                return [dict(row) for row in cur.fetchall()]
        except Exception as e:
            logger.debug("get_changes hibára futott: %s", e)
            return []

    # ---------------- BACKUP / RESTORE ----------------
    def _backup_lock_path(self) -> Path:
        return self.backup_dir / (self.db_path.name + ".backup.lock")

    def _timestamped_backup_name(self) -> Path:
        return self.backup_dir / f"{self.db_path.stem}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.db.bak"

    def list_backups(self, limit: int = 100) -> List[Path]:
        files = sorted(self.backup_dir.glob(f"{self.db_path.stem}_*.db.bak"), key=lambda x: x.stat().st_mtime, reverse=True)
        return files[:limit]

    def make_backup(self, reason: str = "manual", keep: int = 20) -> Optional[Path]:
        if not self.db_path.exists():
            logger.debug("DB fájl nem létezik, nem készítünk backupot: %s", self.db_path)
            return None
        lock_path = self._backup_lock_path()
        fd = None
        try:
            fd = create_lockfile(lock_path, timeout_sec=20)
            bak = self._timestamped_backup_name()
            with closing(sqlite3.connect(str(self.db_path))) as source:
                with closing(sqlite3.connect(str(bak))) as destination:
                    source.backup(destination)
            # rotate
            all_backups = sorted(self.backup_dir.glob(f"{self.db_path.stem}_*.db.bak"), key=lambda x: x.stat().st_mtime, reverse=True)
            for old in all_backups[keep:]:
                try:
                    old.unlink()
                except Exception:
                    pass
            logger.info("Backup készítve: %s (reason=%s)", bak, reason)
            return bak
        finally:
            if fd is not None:
                try:
                    release_lockfile(fd, lock_path)
                except Exception:
                    pass

    def restore_backup(self, backup_path: Path) -> bool:
        backup_path = Path(backup_path)
        if not backup_path.exists():
            raise FileNotFoundError(str(backup_path))
        lock_path = self._backup_lock_path()
        fd = None
        try:
            fd = create_lockfile(lock_path, timeout_sec=30)
            with closing(sqlite3.connect(backup_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise sqlite3.DatabaseError("A biztonsági mentés sérült.")
                with self.get_connection() as destination:
                    source.backup(destination)
            logger.info("Backup visszaállítva: %s", backup_path)
            return True
        finally:
            if fd is not None:
                try:
                    release_lockfile(fd, lock_path)
                except Exception:
                    pass

    # egyszerű esemény log
    def log_event(self, note: Optional[str] = None) -> None:
        with self.get_connection() as conn:
            conn.execute("INSERT INTO db_events (ts, note) VALUES (?, ?)", (int(time.time()), note or ""))