import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

from core.db import Database, MAX_RETRIES, SCHEMA_VERSION, fcntl, retry_db
from utils.reviews import review_dates


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / (".database-test-" + uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = self.directory / "database.db"
        self.backups = self.directory / "backups"

    def database(self, **kwargs):
        db = Database(self.path, self.backups, **kwargs)
        self.addCleanup(db.close)
        return db

    def legacy(self, *, explicit_validity=True):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE telephelyek (id TEXT PRIMARY KEY, nev TEXT NOT NULL)")
            validity = ", hataly_tipus TEXT" if explicit_validity else ""
            conn.execute("""
                CREATE TABLE engedelyek (
                    id TEXT PRIMARY KEY,
                    telephely_id TEXT REFERENCES telephelyek(id),
                    lejarat TEXT, hatarozatlan INTEGER,
                    felulvizsgalat_nap INTEGER, felulvizsgalat_date TEXT,
                    felulvizsgalatok TEXT
                """ + validity + ")")

    def test_success_commits_and_error_rolls_back_all_writes(self):
        db = self.database()
        initial = db.get_data_version()
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with db.get_connection() as conn:
                conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('a', 'A')")
                conn.execute("INSERT INTO engedelyek (id, telephely_id) VALUES ('p', 'a')")
                raise RuntimeError("abort")
        self.assertEqual(db.get_all_data(), {"telephelyek": [], "engedelyek": []})
        self.assertEqual(db.get_data_version(), initial)
        with db.get_connection() as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('a', 'A')")
        self.assertEqual(len(db.get_telephelyek()), 1)

    def test_integrity_failure_rolls_back_earlier_writes(self):
        db = self.database()
        with self.assertRaises(sqlite3.IntegrityError):
            with db.get_connection() as conn:
                conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('a', 'A')")
                conn.execute("INSERT INTO engedelyek (id, telephely_id) VALUES ('p', 'missing')")
        self.assertEqual(db.get_telephelyek(), [])

    def test_foreign_keys_enabled_for_site_and_responsible_user(self):
        db = self.database()
        with db.get_connection() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertIsNotNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='users'").fetchone())
        with self.assertRaises(sqlite3.IntegrityError):
            with db.get_connection() as conn:
                conn.execute("INSERT INTO engedelyek (id, responsible_user_id) VALUES ('p', 'missing')")
        with db.get_connection() as conn:
            conn.execute("INSERT INTO users (id, username) VALUES ('u', 'Admin')")
            conn.execute("INSERT INTO engedelyek (id, responsible_user_id) VALUES ('p', 'u')")
        with self.assertRaises(sqlite3.IntegrityError):
            with db.get_connection() as conn:
                conn.execute("DELETE FROM users WHERE id='u'")
        with self.assertRaises(sqlite3.IntegrityError):
            with db.get_connection() as conn:
                conn.execute("INSERT INTO users (id, username) VALUES ('other', 'ADMIN')")

    def test_migrations_preserve_explicit_validity_and_malformed_reviews(self):
        self.legacy()
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                INSERT INTO engedelyek VALUES
                ('legacy', 'orphan', '2030-01-31', 0, 30, 'bad-date',
                 '{broken JSON', 'until_modified')
            """)
        db = self.database()
        row = db.get_all_data()["engedelyek"][0]
        self.assertEqual(row["hataly_tipus"], "until_modified")
        self.assertEqual(row["felulvizsgalatok"], "{broken JSON")
        self.assertEqual(row["felulvizsgalat_date"], "bad-date")
        self.assertEqual(row["telephely_id"], "orphan")
        self.assertEqual(row["version"], 1)
        with db.get_connection() as conn:
            self.assertEqual(len(conn.execute("PRAGMA foreign_key_check").fetchall()), 1)
        reopened = self.database()
        self.assertEqual(reopened.get_all_data(), db.get_all_data())

    def test_legacy_validity_and_review_backfill(self):
        self.legacy(explicit_validity=False)
        with sqlite3.connect(self.path) as conn:
            conn.executemany("INSERT INTO engedelyek VALUES (?, NULL, ?, ?, ?, NULL, NULL)", [
                ("fixed", "2030-01-31", 0, 30),
                ("forever", "9999-12-31", 0, None),
                ("flag", None, 1, None),
                ("modified", None, 0, None),
                ("invalid", "not-a-date", 0, "not-a-number"),
            ])
        rows = {row["id"]: row for row in self.database().get_all_data()["engedelyek"]}
        self.assertEqual(rows["fixed"]["hataly_tipus"], "fixed")
        self.assertEqual(rows["forever"]["hataly_tipus"], "indefinite")
        self.assertEqual(rows["flag"]["hataly_tipus"], "indefinite")
        self.assertEqual(rows["modified"]["hataly_tipus"], "until_modified")
        self.assertIsNone(rows["fixed"]["felulvizsgalat_date"])
        self.assertEqual(json.loads(rows["fixed"]["felulvizsgalatok"]),
                         [{"type": "before_expiry", "days": 30, "note": ""}])
        self.assertEqual(str(review_dates(rows["fixed"])[0]), "2030-01-01")
        rows["fixed"]["lejarat"] = "2031-01-31"
        self.assertEqual(str(review_dates(rows["fixed"])[0]), "2031-01-01")
        self.assertIsNone(rows["invalid"]["felulvizsgalatok"])

    def test_explicit_zero_relative_review_and_independent_date_are_preserved(self):
        self.legacy()
        with sqlite3.connect(self.path) as conn:
            conn.executemany("INSERT INTO engedelyek VALUES (?, NULL, ?, 0, ?, ?, NULL, 'fixed')", [
                ("zero", "2030-01-31", 0, None),
                ("independent", "2030-01-31", 30, "2030-01-01"),
                ("no-expiry", None, 0, None),
            ])
        rows = {row["id"]: row for row in self.database().get_all_data()["engedelyek"]}
        self.assertEqual(json.loads(rows["zero"]["felulvizsgalatok"]),
                         [{"type": "before_expiry", "days": 0, "note": ""}])
        self.assertEqual(json.loads(rows["no-expiry"]["felulvizsgalatok"]),
                         [{"type": "before_expiry", "days": 0, "note": ""}])
        self.assertEqual(json.loads(rows["independent"]["felulvizsgalatok"]), [
            {"type": "date", "date": "2030-01-01", "note": ""},
            {"type": "before_expiry", "days": 30, "note": ""},
        ])
        self.assertEqual(rows["independent"]["felulvizsgalat_date"], "2030-01-01")

    def test_failed_startup_backup_prevents_all_migration_and_journal_changes(self):
        self.legacy()
        with patch.object(Database, "make_backup", side_effect=OSError("backup unavailable")):
            with patch.object(Database, "_configure_journal") as journal:
                with patch.object(Database, "migrate_db") as migrate:
                    with self.assertRaisesRegex(OSError, "backup unavailable"):
                        self.database()
        journal.assert_not_called()
        migrate.assert_not_called()
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertNotIn("version", [row[1] for row in conn.execute("PRAGMA table_info(engedelyek)")])

    def test_existing_empty_json_array_is_preserved(self):
        self.legacy()
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                INSERT INTO engedelyek VALUES
                ('p', NULL, '2030-01-31', 0, 30, NULL, '[]', 'indefinite')
            """)
        row = self.database().get_all_data()["engedelyek"][0]
        self.assertEqual(row["felulvizsgalatok"], "[]")
        self.assertEqual(row["hataly_tipus"], "indefinite")

    def test_whitespace_malformed_review_json_is_preserved_verbatim(self):
        self.legacy()
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                INSERT INTO engedelyek VALUES
                ('p', NULL, '2030-01-31', 0, 30, NULL, '  ', 'fixed')
            """)
        self.assertEqual(self.database().get_all_data()["engedelyek"][0]["felulvizsgalatok"], "  ")

    def test_failed_migration_rolls_back_schema_and_version(self):
        with patch.object(Database, "_migrate_revisions", side_effect=RuntimeError("migration failed")):
            with self.assertRaisesRegex(RuntimeError, "migration failed"):
                self.database()
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        self.database()

    def test_future_schema_version_refused(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(RuntimeError, "újabb"):
            self.database()
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION + 1)

    def test_migration_idempotence_and_version(self):
        db = self.database()
        with db.get_connection() as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            conn.execute("INSERT INTO engedelyek (id, hataly_tipus) VALUES ('p', 'indefinite')")
        revision = db.get_data_version()
        db.init_db()
        db.migrate_db()
        self.assertEqual(db.get_data_version(), revision)
        self.assertEqual(db.get_all_data()["engedelyek"][0]["hataly_tipus"], "indefinite")

    def test_revision_and_permit_version_survive_connections_and_instances(self):
        db = self.database()
        initial = db.get_data_version()
        with db.get_connection() as conn:
            conn.execute("INSERT INTO engedelyek (id) VALUES ('p')")
        inserted = db.get_data_version()
        self.assertGreater(inserted, initial)
        self.assertEqual(db.get_all_data()["engedelyek"][0]["version"], 1)
        with db.get_connection() as conn:
            conn.execute("UPDATE engedelyek SET megjegyzes='changed' WHERE id='p'")
        self.assertEqual(db.get_all_data()["engedelyek"][0]["version"], 2)
        with db.get_connection() as conn:
            conn.execute("UPDATE engedelyek SET version=version+1 WHERE id='p' AND version=2")
        self.assertEqual(db.get_all_data()["engedelyek"][0]["version"], 3)
        with db.get_connection() as conn:
            self.assertEqual(conn.execute(
                "UPDATE engedelyek SET version=version+1 WHERE id='p' AND version=2").rowcount, 0)
        updated = db.get_data_version()
        self.assertGreater(updated, inserted)
        self.assertEqual(self.database().get_data_version(), updated)
        with db.get_connection() as conn:
            conn.execute("DELETE FROM engedelyek WHERE id='p'")
        self.assertGreater(db.get_data_version(), updated)

    def test_revision_tracks_site_and_user_changes(self):
        db = self.database()
        revision = db.get_data_version()
        with db.get_connection() as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('s', 'Site')")
            conn.execute("INSERT INTO users (id, username) VALUES ('u', 'name')")
        self.assertEqual(db.get_data_version(), revision + 2)

    def test_site_version_supports_optimistic_updates(self):
        db = self.database()
        with db.get_connection(immediate=True) as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('s', 'Site')")
        self.assertEqual(db.get_telephelyek()[0]["version"], 1)
        with db.get_connection(immediate=True) as conn:
            self.assertEqual(conn.execute(
                "UPDATE telephelyek SET nev='Changed', version=version+1 WHERE id='s' AND version=1"
            ).rowcount, 1)
        with db.get_connection(immediate=True) as conn:
            self.assertEqual(conn.execute(
                "UPDATE telephelyek SET nev='Stale' WHERE id='s' AND version=1"
            ).rowcount, 0)
            conn.execute("UPDATE telephelyek SET cim='New address' WHERE id='s'")
        self.assertEqual(db.get_all_data()["telephelyek"][0]["version"], 3)

    def test_version_two_database_gains_site_version_without_data_loss(self):
        with patch.object(Database, "_migrate_site_versions"):
            db = self.database()
        with db.get_connection() as conn:
            conn.execute("PRAGMA user_version=2")
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('s', 'Legacy site')")
        revision = db.get_data_version()
        upgraded = self.database()
        self.assertEqual(upgraded.get_telephelyek()[0]["version"], 1)
        self.assertEqual(upgraded.get_telephelyek()[0]["nev"], "Legacy site")
        self.assertEqual(upgraded.get_data_version(), revision)

    def test_default_delete_and_explicit_wal_journals(self):
        db = self.database()
        with db.get_connection() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        db = self.database(journal_mode="WAL")
        with db.get_connection() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        with self.assertRaises(ValueError):
            self.database(journal_mode="OFF")

    def test_read_snapshot_is_consistent_across_tables(self):
        db = self.database(journal_mode="WAL")
        original_connect = db._connect
        written = False

        def connect():
            conn = original_connect()

            def trace(statement):
                nonlocal written
                if statement.startswith("SELECT * FROM engedelyek") and not written:
                    written = True
                    with original_connect() as writer:
                        writer.execute("BEGIN")
                        writer.execute("INSERT INTO telephelyek (id, nev) VALUES ('s', 'New')")
                        writer.execute("INSERT INTO engedelyek (id, telephely_id) VALUES ('p', 's')")
                        writer.commit()
                    writer.close()

            conn.set_trace_callback(trace)
            return conn

        with patch.object(db, "_connect", side_effect=connect):
            self.assertEqual(db.get_all_data(), {"telephelyek": [], "engedelyek": []})
        self.assertTrue(written)
        self.assertEqual(len(db.get_all_data()["engedelyek"]), 1)

    def test_read_api_soft_deletes_and_changes(self):
        db = self.database()
        with db.get_connection() as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('s', 'Site')")
            conn.executemany(
                "INSERT INTO engedelyek (id, deleted, last_modified) VALUES (?, ?, ?)",
                [("a", 0, "2030-01-01"), ("b", 1, "2030-01-02")])
        self.assertEqual([row["id"] for row in db.get_changes(None)], ["a"])
        self.assertEqual([row["id"] for row in db.get_changes("2029-01-01")], ["a", "b"])
        changes = db.get_changes("2030-01-01")
        self.assertEqual([row["id"] for row in changes], ["b"])
        self.assertEqual(changes[0]["deleted"], 1)

    def test_backup_captures_wal_data_and_is_valid(self):
        db = self.database(journal_mode="WAL")
        keeper = db._connect()
        self.addCleanup(keeper.close)
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("INSERT INTO telephelyek (id, nev) VALUES ('s', 'Committed in WAL')")
        self.assertTrue(Path(str(self.path) + "-wal").exists())
        backup = db.make_backup()
        with sqlite3.connect(backup) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT nev FROM telephelyek").fetchone()[0],
                             "Committed in WAL")
        self.assertEqual(list(self.backups.glob("*.partial")), [])

    def test_backup_unique_names_and_rotation(self):
        db = self.database()
        first = db.make_backup(keep=2)
        second = db.make_backup(keep=2)
        third = db.make_backup(keep=2)
        self.assertEqual(len({first, second, third}), 3)
        self.assertFalse(first.exists())
        self.assertEqual(set(db.list_backups()), {second, third})
        self.assertEqual(db.list_backups(limit=0), [])
        with self.assertRaises(ValueError):
            db.make_backup(keep=0)

    def test_backup_does_not_overwrite_collision(self):
        db = self.database()
        target = self.backups / "database_collision.db.bak"
        target.write_bytes(b"existing backup")
        with patch.object(db, "_timestamped_backup_name", return_value=target):
            with self.assertRaises(FileExistsError):
                db.make_backup()
        self.assertEqual(target.read_bytes(), b"existing backup")
        self.assertEqual([p for p in self.backups.iterdir() if p.name.endswith(".partial")], [])

    def test_failed_backup_is_not_published_and_cleans_staging(self):
        db = self.database()
        self.path.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            db.make_backup()
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_restore_always_refuses_without_modifying_live_database(self):
        db = self.database(journal_mode="WAL")
        with db.get_connection() as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('s', 'Current')")
        revision = db.get_data_version()
        backup = db.make_backup()
        with self.assertRaisesRegex(RuntimeError, "online visszaállítás.*tiltott"):
            db.restore_backup(backup)
        self.assertEqual(db.get_data_version(), revision)
        self.assertEqual(db.get_telephelyek()[0]["nev"], "Current")

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_offline_restore_requires_operator_assertion_and_no_active_applications(self):
        db = self.database()
        backup = db.make_backup()
        with self.assertRaisesRegex(RuntimeError, "kizárólagos"):
            Database.restore_offline(self.path, backup, precopy_dir=self.backups)
        with self.assertRaisesRegex(RuntimeError, "Aktív"):
            Database.restore_offline(self.path, backup, precopy_dir=self.backups,
                                     exclusive_local=True)
        self.assertEqual(db.get_all_data()["telephelyek"], [])

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_connection_retains_lease_even_if_database_is_closed(self):
        db = self.database()
        backup = db.make_backup()
        with db.get_connection():
            db.close()
            with self.assertRaisesRegex(RuntimeError, "Aktív"):
                Database.restore_offline(self.path, backup, precopy_dir=self.backups,
                                         exclusive_local=True)
        with self.assertRaisesRegex(RuntimeError, "le van zárva"):
            db.get_all_data()

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_offline_restore_validates_precopies_and_restores_with_monotonic_revision(self):
        db = self.database(journal_mode="WAL")
        with db.get_connection() as conn:
            conn.execute("INSERT INTO telephelyek (id, nev) VALUES ('s', 'Before')")
        backup = db.make_backup()
        with db.get_connection() as conn:
            conn.execute("UPDATE telephelyek SET nev='After' WHERE id='s'")
        revision = db.get_data_version()
        db.close()
        precopy = Database.restore_offline(
            self.path, backup, precopy_dir=self.directory / "recovery", exclusive_local=True)
        with sqlite3.connect(precopy) as conn:
            self.assertEqual(conn.execute("SELECT nev FROM telephelyek").fetchone()[0], "After")
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        restored = self.database()
        self.assertEqual(restored.get_telephelyek()[0]["nev"], "Before")
        self.assertGreater(restored.get_data_version(), revision)
        self.assertFalse(Path(str(self.path) + "-wal").exists())
        self.assertEqual([p for p in self.directory.iterdir() if p.name.endswith(".restore")], [])

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_offline_restore_revokes_sessions_versions_users_and_records_recovery_audit(self):
        db = self.database()
        with db.get_connection() as conn:
            conn.execute("""
                CREATE TABLE auth_sessions(
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    auth_version INTEGER NOT NULL, expires_at REAL NOT NULL)
            """)
            conn.execute("""
                CREATE TABLE login_throttle(
                    username TEXT PRIMARY KEY, failures INTEGER NOT NULL, window_start REAL NOT NULL)
            """)
            conn.execute("""
                CREATE TABLE audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                    actor_id TEXT, action TEXT NOT NULL, entity_id TEXT, details TEXT NOT NULL)
            """)
            conn.execute("""
                INSERT INTO users(id,username,display_name,password_hash,role,active,auth_version)
                VALUES('u','operator','Operator','unused','admin',1,5)
            """)
            conn.execute("INSERT INTO auth_sessions VALUES('legacy-session','u',5,9999999999)")
        backup = db.make_backup()
        db.close()
        precopy = Database.restore_offline(
            self.path, backup, precopy_dir=self.directory / "recovery", exclusive_local=True)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT auth_version FROM users").fetchone()[0], 6)
            event = conn.execute(
                "SELECT actor_id,action,entity_id,details FROM audit_log ORDER BY id DESC").fetchone()
            self.assertEqual(event[:3], (None, "offline_restore", None))
            self.assertEqual(json.loads(event[3]),
                             {"trusted_os_recovery": True, "sessions_revoked": True})
            self.assertIn("offline_restore", conn.execute(
                "SELECT note FROM db_events ORDER BY id DESC").fetchone()[0])
        for original in (backup, precopy):
            with sqlite3.connect(original) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT auth_version FROM users").fetchone()[0], 5)

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_offline_restore_refuses_invalid_user_role(self):
        db = self.database()
        with db.get_connection() as conn:
            conn.execute("INSERT INTO users(id,username,role,active) VALUES('u','operator','superadmin',1)")
        backup = db.make_backup()
        db.close()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "szerep"):
            Database.restore_offline(self.path, backup, precopy_dir=self.backups,
                                     exclusive_local=True)
        self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_offline_restore_refuses_dangling_responsible_user(self):
        db = self.database()
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO engedelyek(id,responsible_user_id) VALUES('p','missing')")
        backup = db.make_backup()
        db.close()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "felelős"):
            Database.restore_offline(self.path, backup, precopy_dir=self.backups,
                                     exclusive_local=True)
        self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_offline_restore_refuses_legacy_site_orphans(self):
        db = self.database()
        with sqlite3.connect(self.path) as conn:
            conn.execute("INSERT INTO engedelyek(id,telephely_id) VALUES('p','missing')")
        backup = db.make_backup()
        db.close()
        with self.assertRaisesRegex(ValueError, "idegenkulcs"):
            Database.restore_offline(self.path, backup, precopy_dir=self.backups,
                                     exclusive_local=True)

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_offline_restore_refuses_malformed_optional_auth_table_shape(self):
        db = self.database()
        with db.get_connection() as conn:
            conn.execute("""
                CREATE TABLE auth_sessions(
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    auth_version INTEGER NOT NULL, expires_at REAL NOT NULL)
            """)
        backup = db.make_backup()
        db.close()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "hitelesítési séma"):
            Database.restore_offline(self.path, backup, precopy_dir=self.backups,
                                     exclusive_local=True)
        self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_offline_restore_rejects_wrong_schema_without_touching_target(self):
        db = self.database()
        bad = self.directory / "wrong.db"
        with sqlite3.connect(bad) as conn:
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            conn.execute("CREATE TABLE unrelated (id INTEGER)")
        db.close()
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "séma"):
            Database.restore_offline(self.path, bad, precopy_dir=self.backups,
                                     exclusive_local=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual([p for p in self.directory.iterdir() if p.name.endswith(".restore")], [])

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_offline_restore_mandatory_precopy_failure_preserves_target(self):
        db = self.database()
        backup = db.make_backup()
        db.close()
        original = self.path.read_bytes()
        original_backup = Database.make_backup

        def backup_or_fail(instance, reason="manual", keep=20):
            if reason == "mandatory-pre-restore":
                raise OSError("pre-copy failed")
            return original_backup(instance, reason, keep)

        with patch.object(Database, "make_backup", new=backup_or_fail):
            with self.assertRaisesRegex(OSError, "pre-copy failed"):
                Database.restore_offline(self.path, backup, precopy_dir=self.backups,
                                         exclusive_local=True)
        self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_offline_maintenance_lock_refuses_new_database_clients(self):
        db = self.database()
        db.close()
        fd = os.open(Database._lease_path(self.path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "karbantartás"):
                self.database()
        finally:
            os.close(fd)

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_external_sqlite_read_transaction_prevents_offline_replacement(self):
        db = self.database()
        backup = db.make_backup()
        db.close()
        reader = sqlite3.connect(self.path)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM telephelyek").fetchall()
            original = self.path.read_bytes()
            with self.assertRaises(sqlite3.OperationalError):
                Database.restore_offline(self.path, backup, precopy_dir=self.backups,
                                         exclusive_local=True)
            self.assertEqual(self.path.read_bytes(), original)
        finally:
            reader.close()

    @unittest.skipIf(fcntl is None, "Offline recovery requires POSIX locks")
    def test_offline_cli_respects_cross_process_lease_then_restores(self):
        db = self.database()
        backup = db.make_backup()
        command = [
            sys.executable, "-m", "core.db", "offline-restore",
            "--database", str(self.path.resolve()), "--backup", str(backup.resolve()),
            "--precopy-dir", str((self.directory / "recovery").resolve()),
            "--exclusive-local",
        ]
        blocked = subprocess.run(command, capture_output=True, text=True,
                                 cwd=Path(__file__).resolve().parents[1], timeout=10)
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("Aktív", blocked.stderr)
        db.close()
        restored = subprocess.run(command, capture_output=True, text=True,
                                  cwd=Path(__file__).resolve().parents[1], timeout=10)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn("Offline visszaállítás kész", restored.stdout)

    def test_unsupported_hard_link_publication_fails_without_final_backup(self):
        db = self.database()
        with patch("core.db.os.link", side_effect=OSError("unsupported hard links")):
            with self.assertRaisesRegex(OSError, "unsupported"):
                db.make_backup()
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_non_busy_operational_errors_are_not_retried(self):
        calls = []

        @retry_db
        def fail():
            calls.append(1)
            raise sqlite3.OperationalError("no such table: missing")

        with patch("core.db.time.sleep") as sleep:
            with self.assertRaises(sqlite3.OperationalError):
                fail()
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()

    def test_busy_retries_are_bounded_without_final_sleep(self):
        calls = []

        @retry_db
        def fail():
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")

        with patch("core.db.time.sleep") as sleep:
            with self.assertRaises(sqlite3.OperationalError):
                fail()
        self.assertEqual(len(calls), MAX_RETRIES)
        self.assertEqual(sleep.call_count, MAX_RETRIES - 1)
        self.assertLessEqual(sum(call.args[0] for call in sleep.call_args_list), 0.6)

    def test_busy_retry_can_succeed(self):
        attempts = 0

        @retry_db
        def operation():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("database is locked")
            return 42

        with patch("core.db.time.sleep"):
            self.assertEqual(operation(), 42)
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
