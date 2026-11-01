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

    def test_migration_backfill_and_validity_preservation(self):
        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO engedelyek (id, hataly_tipus, lejarat, felulvizsgalat_nap) "
                "VALUES ('permit', 'until_modified', '2030-12-31', 30)"
            )
        self.db.migrate_db()
        permit = self.db.get_all_data()["engedelyek"][0]
        self.assertEqual(permit["hataly_tipus"], "until_modified")
        self.assertEqual(permit["felulvizsgalat_date"], "2030-12-01")
        self.assertIn('"type": "date"', permit["felulvizsgalatok"])

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
