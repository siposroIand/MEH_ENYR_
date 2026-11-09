import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core.db import Database
from core.permit_details import DETAIL_COLUMNS, LEGACY_DETAIL_COLUMNS, load_details


class PermitDetailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "records.db")
        self.data = {
            "kategoria": "Egyedi engedély", "kiado_hatosag": "Hatóság",
            "hataly_tipus": "fixed", "lejarat": "2030-12-31",
            "felulvizsgalatok": [
                {"type": "before_expiry", "days": 30, "note": "Relatív"},
                {"type": "date", "date": "2028-12-01", "note": "Egyszeri"},
            ],
            "modositok": ["MOD/1", "MOD;2"],
            "hulladek_nv_elokez": "1,25 kg", "hulladek_v_gyujt": "régi szabad szöveg",
        }

    def test_round_trip_uses_child_tables_and_incremental_reads(self):
        self.db.save_permit(self.data, "permit", "2026-01-01T00:00:00")
        with self.db.get_connection() as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(engedelyek)")}
            self.assertFalse(columns & LEGACY_DETAIL_COLUMNS)
            self.assertEqual([tuple(row) for row in conn.execute(
                "SELECT tipus, mennyiseg, egyseg FROM hulladekadatok ORDER BY tipus"
            )], [
                ("hulladek_nv_elokez", "1,25", "kg"),
                ("hulladek_v_gyujt", "régi szabad szöveg", None),
            ])
        record = self.db.get_changes("2026-01-01T00:00:00")[0]
        self.assertEqual(json.loads(record["felulvizsgalatok"]), self.data["felulvizsgalatok"])
        for field in ("modositok", "hulladek_nv_elokez", "hulladek_v_gyujt"):
            self.assertEqual(record[field], self.data[field])
        self.assertEqual(record["felulvizsgalat_date"], "2028-12-01")
        self.assertEqual(record["felulvizsgalat_nap"], 30)
        self.assertEqual(self.db.get_all_data()["engedelyek"], [record])
        with self.db.get_connection() as conn:
            conn.execute("UPDATE engedelyek SET deleted=1, last_modified='2026-01-02' WHERE id='permit'")
        self.assertEqual(self.db.get_all_data()["engedelyek"], [])
        deleted = self.db.get_changes("2026-01-02")[0]
        self.assertEqual(deleted["deleted"], 1)
        self.assertEqual(deleted["modositok"], self.data["modositok"])

    def test_parent_and_children_share_a_snapshot_during_concurrent_update(self):
        for incremental in (False, True):
            with self.subTest(incremental=incremental):
                self.db.save_permit(self.data, "permit", "2026-01-01", updating=incremental)

                def concurrent_write(conn, rows):
                    self.db.save_permit(
                        {**self.data, "modositok": ["NEW"], "kategoria": "New type"},
                        "permit", "2026-01-02", updating=True,
                    )
                    return load_details(conn, rows)

                with patch("core.db.load_details", side_effect=concurrent_write):
                    records = self.db.get_changes("2026-01-01") if incremental else self.db.get_all_data()["engedelyek"]
                self.assertEqual(records[0]["kategoria"], self.data["kategoria"])
                self.assertEqual(records[0]["modositok"], self.data["modositok"])
                current = self.db.get_all_data()["engedelyek"][0]
                self.assertEqual(current["kategoria"], "New type")
                self.assertEqual(current["modositok"], ["NEW"])

    def test_update_replaces_children_and_empty_lists_clear_them(self):
        self.db.save_permit(self.data, "permit", "2026-01-01")
        self.db.save_permit({**self.data, "modositok": ["MOD/3"]}, "permit", "2026-01-02", True)
        self.assertEqual(self.db.get_changes("2026-01-02")[0]["modositok"], ["MOD/3"])
        cleared = {**self.data, "modositok": [], "felulvizsgalatok": [],
                   "hulladek_nv_elokez": None, "hulladek_v_gyujt": None}
        self.db.save_permit(cleared, "permit", "2026-01-03", True)
        with self.db.get_connection() as conn:
            for table in DETAIL_COLUMNS:
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.assertEqual(self.db.get_changes("2026-01-03")[0]["felulvizsgalatok"], "[]")

    def test_failed_child_write_rolls_back_parent_children_and_new_catalog(self):
        self.db.save_permit(self.data, "permit", "2026-01-01")
        before = self.db.get_all_data()
        invalid = {**self.data, "kategoria": "Must roll back", "felulvizsgalatok": [{"type": "date", "date": "bad"}]}
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.save_permit(invalid, "permit", "2026-01-02", True)
        self.assertEqual(self.db.get_all_data(), before)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.save_permit(invalid, "new", "2026-01-02")
        self.assertEqual(self.db.get_all_data(), before)

    def test_foreign_keys_cascade_and_backup_restore_children(self):
        self.db.save_permit(self.data, "permit", "2026-01-01")
        before = self.db.get_all_data()
        backup = self.db.make_backup()
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM engedelyek WHERE id='permit'")
            for table in DETAIL_COLUMNS:
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        self.db.restore_backup(backup)
        self.assertEqual(self.db.get_all_data(), before)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.get_connection() as conn:
                conn.execute("INSERT INTO modosito_hatarozatok VALUES ('missing', 0, 'MOD')")

    def test_batch_loading_scales_without_one_query_per_permit(self):
        with self.db.get_connection() as conn:
            conn.executemany("INSERT INTO engedelyek (id) VALUES (?)", ((str(i),) for i in range(1001)))
            conn.executemany("INSERT INTO modosito_hatarozatok VALUES (?, 0, ?)", ((str(i), f"MOD/{i}") for i in range(1001)))
            rows = [dict(row) for row in conn.execute("SELECT * FROM engedelyek")]
            queries = []
            conn.set_trace_callback(queries.append)
            loaded = load_details(conn, rows)
            conn.set_trace_callback(None)
        self.assertEqual(len(queries), 9)
        self.assertEqual(len(loaded), 1001)
        self.assertTrue(all(row["modositok"] == [f"MOD/{row['id']}"] for row in loaded))

    def test_types_can_be_registered_without_permits_and_reused(self):
        identifier = self.db.add_permit_type(" Új típus ")
        self.assertEqual(identifier, self.db.add_permit_type("Új típus"))
        self.assertEqual(self.db.get_all_data()["engedelyek"], [])
        self.assertEqual(Database(self.db.db_path).get_catalogs()["engedelytipusok"], [{"id": identifier, "nev": "Új típus"}])
        with self.assertRaises(ValueError):
            self.db.add_permit_type("   ")
        self.db.save_permit({**self.data, "kategoria": "Új típus"}, "permit", "2026-01-01")
        self.assertEqual(self.db.get_all_data()["engedelyek"][0]["engedelytipus_id"], identifier)
