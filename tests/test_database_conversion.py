import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from convert_database import convert_database
from core.db import Database


class ConversionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "old.db"
        self.target = self.root / "new.db"
        with sqlite3.connect(self.source) as conn:
            conn.executescript("""
                CREATE TABLE telephelyek (id TEXT PRIMARY KEY, nev TEXT, ktj TEXT, cim TEXT);
                CREATE TABLE engedelyek (
                    id TEXT PRIMARY KEY, telephely_id TEXT, engedely_nev TEXT,
                    kategoria TEXT, kiado_hatosag TEXT, hatarozatszam TEXT,
                    modosito_hat TEXT, letrejott TEXT, hatarozatlan INTEGER,
                    hatarozatlan_szoveg TEXT, lejarat TEXT, felulvizsgalat_nap INTEGER,
                    felulvizsgalat_date TEXT, felulvizsgalatok TEXT, megjegyzes TEXT,
                    archivalt INTEGER, hulladek_nv_elokez TEXT, hulladek_v_elokez TEXT,
                    hulladek_nv_gyujt TEXT, hulladek_v_gyujt TEXT,
                    last_modified TEXT, deleted INTEGER, hataly_tipus TEXT
                );
                CREATE TABLE db_events (id INTEGER PRIMARY KEY, ts INTEGER, note TEXT);
                INSERT INTO telephelyek VALUES ('site', 'Telephely', '123', 'Cím');
            """)
            columns = [r[1] for r in conn.execute("PRAGMA table_info(engedelyek)")]
            self.original = dict(zip(columns, (
                "permit", "site", "Tárgy", "Egyedi engedély", "Hatóság", "PE/123-4/2026",
                "MOD/1", "2026-01-01", 0, "szöveg", "2030-12-31", 30,
                None, None, "Megjegyzés", 1, "1", "2", "3", "4", "2026-01-01T10:00:00", 0, "until_modified",
            )))
            conn.execute(
                f"INSERT INTO engedelyek ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                tuple(self.original.values()),
            )

    def test_preserves_business_data_ids_and_backfills_reviews(self):
        original_bytes = self.source.read_bytes()
        counts = convert_database(self.source, self.target)
        self.assertEqual(self.source.read_bytes(), original_bytes)
        self.assertEqual(counts, {"telephelyek": 1, "engedelyek": 1, "hatosagok": 1, "engedelytipusok": 1})
        db = Database(self.target, self.root / "backups")
        result = db.get_all_data()["engedelyek"][0]
        for key, value in self.original.items():
            if key not in ("felulvizsgalat_date", "felulvizsgalatok"):
                self.assertEqual(result[key], value, key)
        self.assertEqual(result["felulvizsgalat_date"], "2030-12-01")
        self.assertEqual(json.loads(result["felulvizsgalatok"])[0]["date"], "2030-12-01")
        self.assertEqual(db.get_telephelyek()[0]["id"], "site")
        with db.get_connection() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"telephelyek", "engedelyek", "hatosagok", "engedelytipusok"})
            columns = {r[1] for r in conn.execute("PRAGMA table_info(engedelyek)")}
            self.assertNotIn("kategoria", columns)
            self.assertNotIn("kiado_hatosag", columns)

    def test_shared_catalogs_archived_and_deleted_records_survive(self):
        with sqlite3.connect(self.source) as conn:
            conn.execute(
                "INSERT INTO engedelyek (id, telephely_id, kategoria, kiado_hatosag, deleted) "
                "VALUES ('deleted', 'site', ' Egyedi engedély ', ' Hatóság ', 1)"
            )
        counts = convert_database(self.source, self.target)
        self.assertEqual(counts["engedelyek"], 2)
        self.assertEqual(counts["hatosagok"], 1)
        self.assertEqual(counts["engedelytipusok"], 1)
        with sqlite3.connect(self.target) as conn:
            self.assertEqual(conn.execute("SELECT deleted FROM engedelyek WHERE id='deleted'").fetchone()[0], 1)

    def test_very_old_schema_supported_and_explicit_json_preserved(self):
        old = self.root / "minimal.db"
        with sqlite3.connect(old) as conn:
            conn.executescript("""
                CREATE TABLE telephelyek (id TEXT PRIMARY KEY, nev TEXT);
                CREATE TABLE engedelyek (id TEXT PRIMARY KEY, hatarozatlan INTEGER, lejarat TEXT);
                INSERT INTO engedelyek VALUES ('indefinite', 1, NULL), ('fixed', 0, '2030-01-01'), ('until', 0, NULL);
            """)
        convert_database(old, self.target)
        data = Database(self.target).get_all_data()
        self.assertEqual(
            {r["id"]: r["hataly_tipus"] for r in data["engedelyek"]},
            {"indefinite": "indefinite", "fixed": "fixed", "until": "until_modified"},
        )
        reviews = '[{"type":"before_expiry","days":60,"note":"Saját"}]'
        with sqlite3.connect(self.source) as conn:
            conn.execute("UPDATE engedelyek SET felulvizsgalatok=?", (reviews,))
        other = self.root / "reviews.db"
        convert_database(self.source, other)
        self.assertEqual(Database(other).get_all_data()["engedelyek"][0]["felulvizsgalatok"], reviews)

    def test_source_wal_is_included(self):
        with sqlite3.connect(self.source) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("UPDATE engedelyek SET megjegyzes='WAL value'")
            conn.commit()
            convert_database(self.source, self.target)
            self.assertEqual(Database(self.target).get_all_data()["engedelyek"][0]["megjegyzes"], "WAL value")

    def test_existing_destination_and_in_place_conversion_refused(self):
        before = self.source.read_bytes()
        with self.assertRaises(ValueError):
            convert_database(self.source, self.source)
        self.target.write_bytes(b"do not replace")
        with self.assertRaises(FileExistsError):
            convert_database(self.source, self.target)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(self.target.read_bytes(), b"do not replace")

    def test_invalid_reference_aborts_without_partial_output(self):
        with sqlite3.connect(self.source) as conn:
            conn.execute("UPDATE engedelyek SET telephely_id='missing'")
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "Hiányzó telephely"):
            convert_database(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source.read_bytes(), before)
        self.assertFalse(list(self.root.glob(".db-conversion-*")))

    def test_deleted_orphan_is_retained_with_explicit_warning(self):
        with sqlite3.connect(self.source) as conn:
            conn.execute("UPDATE engedelyek SET telephely_id='missing', deleted=1")
        with self.assertLogs("convert_database", level="WARNING") as logs:
            convert_database(self.source, self.target)
        self.assertIn("hiányzó telephelyhivatkozása", "\n".join(logs.output))
        with sqlite3.connect(self.target) as conn:
            self.assertEqual(conn.execute("SELECT id, telephely_id FROM engedelyek").fetchone(), ("permit", None))

    def test_unknown_columns_refused_instead_of_silently_discarded(self):
        with sqlite3.connect(self.source) as conn:
            conn.execute("ALTER TABLE engedelyek ADD COLUMN custom_data TEXT")
        with self.assertRaisesRegex(ValueError, "Nem támogatott engedélyoszlopok"):
            convert_database(self.source, self.target)
        self.assertFalse(self.target.exists())

    def test_publication_failure_leaves_no_partial_file(self):
        with patch("convert_database.os.link", side_effect=OSError("publication failed")):
            with self.assertRaises(OSError):
                convert_database(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.root.glob(".db-conversion-*")))

    def test_app_and_restore_reject_legacy_without_modifying_it(self):
        before = self.source.read_bytes()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "convert_database.py"):
            Database(self.source)
        self.assertEqual(before, self.source.read_bytes())
        db = Database(self.target)
        with self.assertRaises(sqlite3.DatabaseError):
            db.restore_backup(self.source)
        self.assertEqual(db.get_telephelyek(), [])
        self.assertEqual(before, self.source.read_bytes())

    def test_cli_works_without_site_packages(self):
        script = Path(__file__).resolve().parents[1] / "convert_database.py"
        result = subprocess.run(
            [sys.executable, "-S", str(script), str(self.source), str(self.target)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("engedelyek: 1", result.stdout)
        result = subprocess.run(
            [sys.executable, "-S", str(script), str(self.source), str(self.target)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("már létezik", result.stderr)
