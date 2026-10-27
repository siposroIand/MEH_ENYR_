#!/usr/bin/env python3
"""SQLite lifecycle and offline recovery.

Offline recovery (POSIX, trusted local filesystem only): stop every old and
current client, revoke other users' access to the database directory, then run::

    python -m core.db offline-restore --database /local/app.db \
        --backup /local/backups/known-good.db.bak --precopy-dir /local/recovery \
        --exclusive-local

The flag is an operator assertion, not automatic detection of external clients
or network mounts. Never use it on a live share. Cooperative clients hold shared
OS leases; recovery requires an exclusive lease, validates the source schema
and integrity, makes a mandatory validated pre-copy, checkpoints WAL, obtains a
SQLite exclusive lock, and only then publishes the closed replacement. Keep
directory access revoked throughout; restore ACLs and restart clients afterward.
Older clients with no lease and idle external SQLite connections cannot be
detected reliably. If exclusivity cannot be guaranteed, do not restore.

Backups require same-filesystem hard-link publication; unsupported SMB/link
implementations fail safely rather than fall back to overwriting a backup.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import functools
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from typing import Optional, List, Dict, Any
from uuid import uuid4
import weakref

from utils.reviews import parse_reviews

try:
    import fcntl
except ImportError:
    fcntl = None

logger = logging.getLogger(__name__)

MAX_RETRIES = 4
RETRY_INTERVAL = 0.05
SCHEMA_VERSION = 4


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    code = getattr(exc, "sqlite_errorcode", None)
    if code is not None:
        return (code & 0xFF) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return str(exc).lower() in ("database is locked", "database table is locked",
                                "database schema is locked")


def retry_db(func):
    """Retry only transient SQLite lock errors, with a bounded delay."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        for attempt in range(MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc) or attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(min(RETRY_INTERVAL * (2 ** attempt), 0.2))
    return wrapper


class Database:
    def __init__(self, db_path: Path, backup_dir: Optional[Path] = None,
                 *, journal_mode: str = "DELETE"):
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.db_path.parent
        self.journal_mode = journal_mode.upper()
        if self.journal_mode not in ("DELETE", "WAL"):
            raise ValueError("A naplózási mód csak DELETE vagy WAL lehet.")
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._lease_finalizer = None
        self._acquire_application_lease()
        try:
            if self.db_path.exists():
                self.make_backup(reason="startup")
            self._configure_journal()
            self.migrate_db()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _lease_path(db_path):
        return Path(str(Path(db_path).resolve()) + ".application.lock")

    def _acquire_application_lease(self):
        # Offline recovery is unavailable without POSIX shared/exclusive leases.
        fd = self._shared_lease(self.db_path)
        if fd is not None:
            self._lease_finalizer = weakref.finalize(self, os.close, fd)

    @classmethod
    def _shared_lease(cls, db_path):
        if fcntl is None:
            return None
        fd = os.open(cls._lease_path(db_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise RuntimeError("Az adatbázison kizárólagos offline karbantartás folyik.")
        return fd

    def close(self):
        """Release this application's lease; the Database must not be reused."""
        self._closed = True
        if self._lease_finalizer is not None:
            self._lease_finalizer()

    def _ensure_open(self):
        if getattr(self, "_closed", False):
            raise RuntimeError("Az adatbázis-példány már le van zárva.")

    def _connect(self):
        self._ensure_open()
        conn = sqlite3.connect(str(self.db_path), timeout=0.25, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=250")
            conn.execute("PRAGMA synchronous=FULL")
        except BaseException:
            conn.close()
            raise
        return conn

    @retry_db
    def _configure_journal(self):
        conn = self._connect()
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if mode.upper() != self.journal_mode:
                actual = conn.execute(
                    "PRAGMA journal_mode=" + self.journal_mode
                ).fetchone()[0]
                if actual.upper() != self.journal_mode:
                    raise RuntimeError("Az adatbázis naplózási módja nem állítható be.")
        finally:
            conn.close()

    @contextmanager
    def get_connection(self, *, immediate: bool = False):
        """One transaction; callers must not commit inside this context."""
        self._ensure_open()
        lease = self._shared_lease(self.db_path)
        conn = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()
            if lease is not None:
                os.close(lease)

    def init_db(self) -> None:
        self.migrate_db()

    @retry_db
    def migrate_db(self) -> None:
        # Serialize schema inspection and changes across simultaneous starts.
        with self.get_connection(immediate=True) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError("Az adatbázis verziója újabb az alkalmazásnál.")
            if version < 1:
                self._migrate_schema(conn)
                self._migrate_reviews(conn)
                conn.execute("PRAGMA user_version=1")
            if version < 2:
                self._migrate_revisions(conn)
                conn.execute("PRAGMA user_version=2")
            if version < 3:
                self._migrate_site_versions(conn)
                conn.execute("PRAGMA user_version=3")
            if version < 4:
                self._migrate_reviews(conn)
                conn.execute("PRAGMA user_version=4")
            # Existing orphan rows are preserved, not silently deleted or rewritten.
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                logger.warning("Örökölt idegenkulcs-hibák száma: %d", len(violations))

    @staticmethod
    def _migrate_schema(conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE COLLATE NOCASE,
                display_name TEXT,
                password_hash TEXT,
                role TEXT,
                active INTEGER,
                auth_version INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telephelyek (
                id TEXT PRIMARY KEY, nev TEXT NOT NULL, ktj TEXT, cim TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS engedelyek (
                id TEXT PRIMARY KEY,
                telephely_id TEXT REFERENCES telephelyek(id)
            )
        """)
        columns = {
            "engedely_nev": "TEXT", "kategoria": "TEXT", "kiado_hatosag": "TEXT",
            "hatarozatszam": "TEXT", "modosito_hat": "TEXT", "letrejott": "TEXT",
            "hatarozatlan": "INTEGER DEFAULT 0", "hatarozatlan_szoveg": "TEXT",
            "lejarat": "TEXT", "felulvizsgalat_nap": "INTEGER",
            "felulvizsgalat_date": "TEXT", "felulvizsgalatok": "TEXT",
            "megjegyzes": "TEXT", "archivalt": "INTEGER DEFAULT 0",
            "hulladek_nv_elokez": "TEXT", "hulladek_v_elokez": "TEXT",
            "hulladek_nv_gyujt": "TEXT", "hulladek_v_gyujt": "TEXT",
            "last_modified": "TEXT", "deleted": "INTEGER DEFAULT 0",
            "hataly_tipus": "TEXT DEFAULT 'fixed'",
            "version": "INTEGER NOT NULL DEFAULT 1",
            "responsible_user_id": "TEXT REFERENCES users(id)",
        }
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(engedelyek)")}
        had_validity = "hataly_tipus" in existing
        for table, additions in (
            ("telephelyek", {"ktj": "TEXT", "cim": "TEXT"}),
            ("engedelyek", columns),
        ):
            present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in additions.items():
                if name not in present:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS db_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, note TEXT
            )
        """)
        conn.execute("UPDATE engedelyek SET deleted=0 WHERE deleted IS NULL")
        for row in conn.execute("SELECT * FROM engedelyek").fetchall():
            row = dict(row)
            if not had_validity or row["hataly_tipus"] is None or not str(row["hataly_tipus"]).strip():
                indefinite = str(row["hatarozatlan"]) == "1" or row["lejarat"] == "9999-12-31"
                validity = "indefinite" if indefinite else (
                    "fixed" if row["lejarat"] and str(row["lejarat"]).strip() else "until_modified"
                )
                conn.execute("UPDATE engedelyek SET hataly_tipus=? WHERE id=?",
                             (validity, row["id"]))

    @staticmethod
    def _migrate_reviews(conn):
        for row in conn.execute(
                "SELECT id, felulvizsgalat_date, felulvizsgalat_nap, felulvizsgalatok FROM engedelyek"
        ).fetchall():
            # Explicit JSON, even malformed JSON or [], remains untouched.
            if row["felulvizsgalatok"] not in (None, ""):
                continue
            entries = []
            if row["felulvizsgalat_date"] not in (None, ""):
                entries.append({"type": "date", "date": row["felulvizsgalat_date"], "note": ""})
            if row["felulvizsgalat_nap"] not in (None, ""):
                entries.append({"type": "before_expiry", "days": row["felulvizsgalat_nap"], "note": ""})
            parsed = parse_reviews({"felulvizsgalatok": entries})
            if entries and parsed.valid:
                conn.execute("UPDATE engedelyek SET felulvizsgalatok=? WHERE id=?",
                             (json.dumps(list(parsed.entries), ensure_ascii=False), row["id"]))

    @staticmethod
    def _migrate_revisions(conn):
        conn.execute("""
            CREATE TABLE database_revision (
                id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL
            )
        """)
        conn.execute("INSERT INTO database_revision VALUES (1, 0)")
        for table in ("telephelyek", "engedelyek", "users"):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(f"""
                    CREATE TRIGGER revision_{table}_{operation.lower()}
                    AFTER {operation} ON {table}
                    BEGIN
                        UPDATE database_revision SET revision=revision+1 WHERE id=1;
                    END
                """)
        conn.execute("""
            CREATE TRIGGER permit_version AFTER UPDATE ON engedelyek
            WHEN NEW.version <= OLD.version
            BEGIN
                UPDATE engedelyek SET version=OLD.version+1 WHERE id=NEW.id;
            END
        """)

    @staticmethod
    def _migrate_site_versions(conn):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(telephelyek)")}
        if "version" not in columns:
            conn.execute("ALTER TABLE telephelyek ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        conn.execute("""
            CREATE TRIGGER site_version AFTER UPDATE ON telephelyek
            WHEN NEW.version <= OLD.version
            BEGIN
                UPDATE telephelyek SET version=OLD.version+1 WHERE id=NEW.id;
            END
        """)

    @retry_db
    def get_data_version(self) -> int:
        with self.get_connection() as conn:
            return int(conn.execute("SELECT revision FROM database_revision WHERE id=1").fetchone()[0])

    @retry_db
    def get_all_data(self) -> Dict[str, List[Dict[str, Any]]]:
        with self.get_connection() as conn:
            sites = [dict(row) for row in conn.execute(
                "SELECT id, nev, ktj, cim, version FROM telephelyek ORDER BY nev")]
            permits = [dict(row) for row in conn.execute(
                "SELECT * FROM engedelyek WHERE (deleted IS NULL OR deleted=0)")]
            return {"telephelyek": sites, "engedelyek": permits}

    @retry_db
    def get_telephelyek(self) -> List[Dict[str, Any]]:
        with self.get_connection() as conn:
            return [dict(row) for row in conn.execute("SELECT id, nev, ktj, cim, version FROM telephelyek")]

    @retry_db
    def get_changes(self, since: Optional[str]) -> List[Dict[str, Any]]:
        """Legacy timestamp feed including tombstones; use full snapshots for sync.

        Hard deletes and equal/coarse timestamps cannot form a reliable cursor.
        The application's supported synchronization API is get_all_data().
        """
        if not since:
            return self.get_all_data()["engedelyek"]
        with self.get_connection() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM engedelyek WHERE last_modified > ? ORDER BY last_modified, id",
                (since,))]

    def _timestamped_backup_name(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        return self.backup_dir / f"{self.db_path.stem}_{stamp}_{uuid4().hex}.db.bak"

    def list_backups(self, limit: int = 100) -> List[Path]:
        files = []
        for path in self.backup_dir.iterdir():
            if not (path.name.startswith(self.db_path.stem + "_")
                    and path.name.endswith(".db.bak")):
                continue
            try:
                if path.is_file():
                    files.append((path.stat().st_mtime_ns, path))
            except FileNotFoundError:
                # Another backup operation may concurrently rotate this entry.
                continue
        files.sort(key=lambda entry: (entry[0], entry[1].name), reverse=True)
        return [path for _, path in files[:max(0, limit)]]

    def make_backup(self, reason: str = "manual", keep: int = 20) -> Optional[Path]:
        self._ensure_open()
        if keep < 1:
            raise ValueError("Legalább egy biztonsági mentést meg kell őrizni.")
        if not self.db_path.exists():
            return None
        target = self._timestamped_backup_name()
        staging = target.with_name("." + target.name + "." + uuid4().hex + ".partial")
        # Exclusive creation and atomic no-overwrite publication, on the same filesystem.
        fd = os.open(staging, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        source = destination = None
        try:
            source = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro",
                                     uri=True, timeout=0.25)
            destination = sqlite3.connect(str(staging))
            deadline = time.monotonic() + 5.0

            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise sqlite3.OperationalError("A biztonsági mentés időkorlátja lejárt.")

            source.backup(destination, pages=256, progress=progress, sleep=0.05)
            result = destination.execute("PRAGMA integrity_check").fetchall()
            if result != [("ok",)]:
                raise sqlite3.DatabaseError("A biztonsági mentés integritásellenőrzése sikertelen.")
            destination.close()
            destination = None
            with staging.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(staging, target)
            logger.info("Biztonsági mentés: %s (%s)", target, reason)
            for old in self.list_backups(limit=2**31 - 1)[keep:]:
                try:
                    old.unlink()
                except OSError as exc:
                    logger.warning("Régi mentés nem törölhető: %s", exc)
            return target
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
            staging.unlink(missing_ok=True)

    def restore_backup(self, backup_path: Path) -> bool:
        raise RuntimeError(
            "Az online visszaállítás biztonsági okból tiltott. "
            "Zárjon be minden alkalmazáspéldányt, és kérjen rendszergazdai, "
            "kizárólagos offline karbantartást; a WAL/SHM fájlokat is kezelni kell."
        )

    @staticmethod
    def _validate_restore_schema(conn):
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise sqlite3.DatabaseError("A visszaállítás forrása sérült.")
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise ValueError("Csak az alkalmazás jelenlegi sémaverziója állítható vissza.")
        required = {
            "users": {"id", "username", "display_name", "password_hash", "role", "active", "auth_version"},
            "telephelyek": {"id", "nev", "ktj", "cim", "version"},
            "engedelyek": {
                "id", "telephely_id", "version", "responsible_user_id", "engedely_nev",
                "kategoria", "kiado_hatosag", "hatarozatszam", "modosito_hat", "letrejott",
                "hatarozatlan", "hatarozatlan_szoveg", "lejarat", "felulvizsgalat_nap",
                "felulvizsgalat_date", "felulvizsgalatok", "megjegyzes", "archivalt",
                "hulladek_nv_elokez", "hulladek_v_elokez", "hulladek_nv_gyujt",
                "hulladek_v_gyujt", "last_modified", "deleted", "hataly_tipus",
            },
            "database_revision": {"id", "revision"},
            "db_events": {"id", "ts", "note"},
        }
        for table, columns in required.items():
            actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not columns.issubset(actual):
                raise ValueError("Hiányos visszaállítási séma: " + table)
        triggers = ["permit_version", "site_version"]
        triggers.extend(f"revision_{table}_{operation}" for table in
                        ("users", "telephelyek", "engedelyek") for operation in
                        ("insert", "update", "delete"))
        for name in triggers:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                            (name,)).fetchone() is None:
                raise ValueError("Hiányos verziókezelés a visszaállítási forrásban.")
        revision = conn.execute("SELECT revision FROM database_revision WHERE id=1").fetchone()
        if revision is None or not isinstance(revision[0], int) or revision[0] < 0:
            raise ValueError("Érvénytelen adatbázis-revízió.")

    @classmethod
    def restore_offline(cls, db_path: Path, backup_path: Path, *,
                        precopy_dir: Path, exclusive_local: bool = False) -> Path:
        """Restore under operator-guaranteed local exclusivity; return pre-copy.

        See this module's recovery procedure. No online caller should use this
        API. Windows/network recovery is deliberately unsupported.
        """
        if not exclusive_local or fcntl is None:
            raise RuntimeError("Offline visszaállításhoz POSIX és igazolt kizárólagos helyi karbantartás kell.")
        db_path, backup_path = Path(db_path).resolve(), Path(backup_path).resolve()
        precopy_dir = Path(precopy_dir).resolve()
        if not db_path.is_file() or not backup_path.is_file():
            raise FileNotFoundError("A céladatbázisnak és a mentésnek is léteznie kell.")
        if db_path.samefile(backup_path):
            raise ValueError("A mentés nem lehet azonos a céladatbázissal.")
        fd = os.open(cls._lease_path(db_path), os.O_CREAT | os.O_RDWR, 0o600)
        staging = db_path.with_name("." + db_path.name + "." + uuid4().hex + ".restore")
        candidate = None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("Aktív alkalmazáspéldány miatt a visszaállítás tiltott.") from exc
            # Create an isolated SQLite snapshot; never copy live database bytes.
            candidate = cls.__new__(cls)
            candidate.db_path = backup_path
            candidate.backup_dir = db_path.parent
            candidate._closed = False
            copied = candidate.make_backup(reason="offline-source", keep=2**31 - 1)
            os.replace(copied, staging)
            with closing(sqlite3.connect(staging)) as conn:
                conn.execute("PRAGMA journal_mode=DELETE")
                cls._validate_restore_schema(conn)
            candidate.db_path = db_path
            candidate.backup_dir = precopy_dir
            precopy_dir.mkdir(parents=True, exist_ok=True)
            precopy = candidate.make_backup(reason="mandatory-pre-restore", keep=2**31 - 1)
            if precopy is None:
                raise RuntimeError("A kötelező visszaállítás előtti mentés nem készült el.")
            target = sqlite3.connect(str(db_path), timeout=0.25, isolation_level=None)
            try:
                checkpoint = target.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint[0] != 0:
                    raise RuntimeError("A WAL nem zárható le; aktív adatbázis-kapcsolat.")
                if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower() != "delete":
                    raise RuntimeError("Az adatbázis nem helyezhető offline naplózási módba.")
                target.execute("BEGIN EXCLUSIVE")
                row = target.execute("SELECT revision FROM database_revision WHERE id=1").fetchone()
                old_revision = row[0] if row else 0
                with closing(sqlite3.connect(staging)) as conn:
                    with conn:
                        conn.execute("UPDATE database_revision SET revision=max(revision, ?)+1 WHERE id=1",
                                     (old_revision,))
                target.commit()
            finally:
                target.close()
            if any(Path(str(db_path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise RuntimeError("Az adatbázis oldalfájljai még léteznek; a visszaállítás tiltott.")
            with staging.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(staging, db_path)
            directory_fd = os.open(db_path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return precopy
        finally:
            staging.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(staging) + suffix).unlink(missing_ok=True)
            os.close(fd)

    @retry_db
    def log_event(self, note: Optional[str] = None) -> None:
        with self.get_connection() as conn:
            conn.execute("INSERT INTO db_events (ts, note) VALUES (?, ?)",
                         (int(time.time()), note or ""))


def _main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["offline-restore"])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--precopy-dir", type=Path, required=True)
    parser.add_argument("--exclusive-local", action="store_true", required=True)
    args = parser.parse_args()
    precopy = Database.restore_offline(args.database, args.backup,
                                      precopy_dir=args.precopy_dir,
                                      exclusive_local=args.exclusive_local)
    print("Offline visszaállítás kész. Előzetes mentés:", precopy)


if __name__ == "__main__":
    _main()
