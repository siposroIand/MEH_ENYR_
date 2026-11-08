#!/usr/bin/env python3
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime
import functools
import logging
from pathlib import Path
from random import uniform
import sqlite3
import time
from typing import Any, Optional

from utils.fs_utils import create_lockfile, release_lockfile

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1
MAX_RETRIES = 3
RETRY_INTERVAL = 0.15

PERMIT_COLUMNS = {
    "id": "TEXT PRIMARY KEY NOT NULL",
    "telephely_id": "TEXT REFERENCES telephelyek(id)",
    "engedely_nev": "TEXT",
    "engedelytipus_id": "INTEGER REFERENCES engedelytipusok(id)",
    "hatosag_id": "INTEGER REFERENCES hatosagok(id)",
    "hatarozatszam": "TEXT",
    "modosito_hat": "TEXT",
    "letrejott": "TEXT",
    "hatarozatlan": "INTEGER DEFAULT 0",
    "hatarozatlan_szoveg": "TEXT",
    "lejarat": "TEXT",
    "felulvizsgalat_nap": "INTEGER",
    "felulvizsgalat_date": "TEXT",
    "felulvizsgalatok": "TEXT",
    "megjegyzes": "TEXT",
    "archivalt": "INTEGER DEFAULT 0",
    "hulladek_nv_elokez": "TEXT",
    "hulladek_v_elokez": "TEXT",
    "hulladek_nv_gyujt": "TEXT",
    "hulladek_v_gyujt": "TEXT",
    "last_modified": "TEXT",
    "deleted": "INTEGER DEFAULT 0",
    "hataly_tipus": "TEXT DEFAULT 'fixed'",
}
PERMIT_SELECT = """
    SELECT e.*, t.nev AS kategoria, h.nev AS kiado_hatosag
    FROM engedelyek e
    LEFT JOIN engedelytipusok t ON t.id=e.engedelytipus_id
    LEFT JOIN hatosagok h ON h.id=e.hatosag_id
"""


def create_schema(conn: sqlite3.Connection) -> None:
    """Create an empty database only; never alter an existing schema."""
    conn.execute("CREATE TABLE telephelyek (id TEXT PRIMARY KEY NOT NULL, nev TEXT NOT NULL, ktj TEXT, cim TEXT)")
    for table in ("hatosagok", "engedelytipusok"):
        conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, nev TEXT NOT NULL UNIQUE CHECK(length(trim(nev)) > 0))")
    columns = ", ".join(f"{name} {definition}" for name, definition in PERMIT_COLUMNS.items())
    conn.execute(f"CREATE TABLE engedelyek ({columns})")
    for column in ("telephely_id", "hatosag_id", "engedelytipus_id"):
        conn.execute(f"CREATE INDEX idx_engedelyek_{column} ON engedelyek ({column})")
    conn.execute("CREATE INDEX idx_engedelyek_changes ON engedelyek (julianday(last_modified))")
    conn.execute("CREATE INDEX idx_engedelyek_active_expiry ON engedelyek (deleted, archivalt, lejarat)")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def validate_schema(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    expected = {"telephelyek", "engedelyek", "hatosagok", "engedelytipusok"}
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION or tables != expected:
        raise sqlite3.DatabaseError(
            "Régi vagy nem támogatott adatbázis. Zárja be az alkalmazást, majd futtassa: "
            "python convert_database.py regi.db uj.db. Az új fájlt állítsa be adatbázisként."
        )
    required = {
        "telephelyek": {"id", "nev", "ktj", "cim"},
        "engedelyek": set(PERMIT_COLUMNS),
        "hatosagok": {"id", "nev"},
        "engedelytipusok": {"id", "nev"},
    }
    for table, columns in required.items():
        if {r[1] for r in conn.execute(f"PRAGMA table_info({table})")} != columns:
            raise sqlite3.DatabaseError(f"Nem támogatott táblaszerkezet: {table}")


def lookup_id(conn: sqlite3.Connection, table: str, name: str | None) -> int | None:
    if table not in ("hatosagok", "engedelytipusok"):
        raise ValueError(f"Nem törzstábla: {table}")
    name = name.strip() if name else ""
    if not name:
        return None
    conn.execute(f"INSERT INTO {table} (nev) VALUES (?) ON CONFLICT(nev) DO NOTHING", (name,))
    return conn.execute(f"SELECT id FROM {table} WHERE nev=?", (name,)).fetchone()[0]


def retry_db(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not any(reason in str(exc).lower() for reason in ("locked", "busy")) or attempt == MAX_RETRIES - 1:
                    raise
                delay = RETRY_INTERVAL * 2 ** attempt
                logger.warning("Az adatbázis foglalt, újrapróbálkozás: %s", exc)
                time.sleep(delay + uniform(0, delay * 0.1))
    return wrapper


class Database:
    def __init__(self, db_path: Path, backup_dir: Optional[Path] = None):
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.db_path.parent
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.db_path.exists() and self.db_path.stat().st_size:
            with closing(sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                validate_schema(conn)
            try:
                self.make_backup(reason="startup")
            except (OSError, sqlite3.Error) as exc:
                logger.warning("Az indulási biztonsági mentés sikertelen: %s", exc)
        self.init_db()

    @contextmanager
    def get_connection(self):
        conn = sqlite3.connect(str(self.db_path), timeout=2.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @retry_db
    def init_db(self) -> None:
        with self.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                create_schema(conn)
            else:
                validate_schema(conn)
            conn.commit()
            conn.execute("PRAGMA journal_mode=WAL")

    @retry_db
    def get_all_data(self) -> dict[str, list[dict[str, Any]]]:
        with self.get_connection() as conn:
            conn.execute("BEGIN")
            return {
                "telephelyek": [dict(r) for r in conn.execute("SELECT * FROM telephelyek ORDER BY nev")],
                "engedelyek": [dict(r) for r in conn.execute(PERMIT_SELECT + " WHERE e.deleted IS NULL OR e.deleted=0")],
                **self.get_catalogs(conn),
            }

    def get_catalogs(self, conn: sqlite3.Connection | None = None) -> dict[str, list[dict[str, Any]]]:
        if conn is None:
            with self.get_connection() as connection:
                return self.get_catalogs(connection)
        return {
            table: [dict(r) for r in conn.execute(f"SELECT id, nev FROM {table} ORDER BY nev")]
            for table in ("hatosagok", "engedelytipusok")
        }

    @retry_db
    def get_telephelyek(self) -> list[dict[str, Any]]:
        with self.get_connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM telephelyek ORDER BY nev")]

    @retry_db
    def get_changes(self, since: Optional[str]) -> list[dict[str, Any]]:
        if not since:
            return self.get_all_data()["engedelyek"]
        with self.get_connection() as conn:
            return [dict(r) for r in conn.execute(
                PERMIT_SELECT + " WHERE julianday(e.last_modified) >= julianday(?)", (since,),
            )]

    def _backup_lock_path(self) -> Path:
        return self.backup_dir / (self.db_path.name + ".backup.lock")

    def _timestamped_backup_name(self) -> Path:
        return self.backup_dir / f"{self.db_path.stem}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.db.bak"

    def list_backups(self, limit: int = 100) -> list[Path]:
        files = sorted(self.backup_dir.glob(f"{self.db_path.stem}_*.db.bak"), key=lambda x: x.stat().st_mtime, reverse=True)
        return files[:limit]

    def make_backup(self, reason: str = "manual", keep: int = 20) -> Optional[Path]:
        if not self.db_path.exists():
            return None
        lock_path = self._backup_lock_path()
        fd = create_lockfile(lock_path, timeout_sec=20)
        try:
            bak = self._timestamped_backup_name()
            with closing(sqlite3.connect(str(self.db_path))) as source:
                with closing(sqlite3.connect(str(bak))) as destination:
                    source.backup(destination)
            for old in self.list_backups(limit=10000)[keep:]:
                try:
                    old.unlink()
                except OSError as exc:
                    logger.warning("Régi mentés törlése sikertelen: %s", exc)
            logger.info("Backup készítve: %s (reason=%s)", bak, reason)
            return bak
        finally:
            release_lockfile(fd, lock_path)

    def restore_backup(self, backup_path: Path) -> bool:
        backup_path = Path(backup_path)
        lock_path = self._backup_lock_path()
        fd = create_lockfile(lock_path, timeout_sec=30)
        try:
            with closing(sqlite3.connect(backup_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
                validate_schema(source)
                if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or source.execute("PRAGMA foreign_key_check").fetchone():
                    raise sqlite3.DatabaseError("A biztonsági mentés sérült vagy hibás hivatkozást tartalmaz.")
                with self.get_connection() as destination:
                    source.backup(destination)
            logger.info("Backup visszaállítva: %s", backup_path)
            return True
        finally:
            release_lockfile(fd, lock_path)

    def log_event(self, note: Optional[str] = None) -> None:
        logger.info("Adatbázis-esemény: %s", note or "")
