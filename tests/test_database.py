from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core.db import Database, retry_db


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "records.db", self.root / "backups")

    def test_transaction_rolls_back_on_error(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.get_connection() as conn:
                conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('site', 'First')")
                conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('site', 'Duplicate')")
        self.assertEqual(self.db.get_telephelyek(), [])

    def test_successful_transaction_is_persisted(self):
        with self.db.get_connection() as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('site', 'Saved')")
        self.assertEqual(self.db.get_telephelyek()[0]["nev"], "Saved")

    def test_backup_includes_uncheckpointed_wal_changes(self):
        with self.db.get_connection() as conn:
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('site', 'WAL data')")
            conn.commit()
            backup = self.db.make_backup()
            with sqlite3.connect(backup) as restored:
                self.assertEqual(restored.execute("SELECT nev FROM telephelyek").fetchone()[0], "WAL data")

    def test_startup_does_not_backfill_or_modify_records(self):
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO engedelyek (id, hataly_tipus, lejarat, felulvizsgalat_nap) "
                "VALUES ('permit', 'until_modified', '2030-12-31', 30)"
            )
        Database(self.db.db_path, self.db.backup_dir)
        permit = self.db.get_all_data()["engedelyek"][0]
        self.assertEqual(permit["hataly_tipus"], "until_modified")
        self.assertIsNone(permit["felulvizsgalat_date"])
        self.assertIsNone(permit["felulvizsgalatok"])

    def test_restore_replaces_wal_state(self):
        with self.db.get_connection() as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('site', 'Original')")
            conn.commit()
            backup = self.db.make_backup()
            conn.execute("UPDATE telephelyek SET nev='Changed'")
            conn.commit()
            self.db.restore_backup(backup)
            self.assertEqual(conn.execute("SELECT nev FROM telephelyek").fetchone()[0], "Original")

    def test_corrupt_restore_does_not_damage_current_database(self):
        corrupt = self.root / "invalid.bak"
        corrupt.write_bytes(b"not sqlite")
        with self.db.get_connection() as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('site', 'Original')")
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.restore_backup(corrupt)
        self.assertEqual(self.db.get_telephelyek()[0]["nev"], "Original")

    def test_rapid_backups_have_distinct_paths(self):
        self.assertNotEqual(self.db.make_backup(), self.db.make_backup())

    def test_non_lock_errors_not_retried(self):
        @retry_db
        def operation():
            raise sqlite3.OperationalError("no such table: missing")

        with patch("core.db.time.sleep") as sleep:
            with self.assertRaises(sqlite3.OperationalError):
                operation()
            sleep.assert_not_called()

    def test_changes_include_deletions_and_equal_utc_timestamps(self):
        with self.db.get_connection() as conn:
            conn.executemany(
                "INSERT INTO engedelyek (id, last_modified, deleted) VALUES (?, ?, ?)",
                [
                    ("old", "2026-01-01T11:59:59", 0),
                    ("equal", "2026-01-01T12:00:00", 0),
                    ("offset", "2026-01-01T13:00:00+01:00", 0),
                    ("deleted", "2026-01-01T12:00:01", 1),
                ],
            )
        self.assertEqual(
            {r["id"] for r in self.db.get_changes("2026-01-01T12:00:00+00:00")},
            {"equal", "offset", "deleted"},
        )

    def test_changes_failure_is_not_reported_as_empty_success(self):
        with patch.object(self.db, "get_connection", side_effect=sqlite3.OperationalError("no such table")):
            with self.assertRaises(sqlite3.OperationalError):
                self.db.get_changes("2026-01-01T12:00:00")

    def test_incremental_query_uses_date_expression_index(self):
        with self.db.get_connection() as conn:
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM engedelyek WHERE julianday(last_modified)>=julianday(?)",
                ("2026-01-01T12:00:00",),
            ).fetchall()
        self.assertIn("idx_engedelyek_changes", " ".join(str(tuple(r)) for r in plan))

    def test_foreign_keys_are_enforced_on_every_connection(self):
        for _ in range(2):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.db.get_connection() as conn:
                    self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                    conn.execute("INSERT INTO engedelyek (id, hatosag_id) VALUES ('bad', 999)")
