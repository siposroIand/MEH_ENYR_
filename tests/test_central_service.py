import concurrent.futures
from http.client import IncompleteRead
import os
from pathlib import Path
import shutil
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.db import ConflictError, Database
from core.db_service import DatabaseServer, MAX_REQUEST_BYTES
from core.remote_db import RemoteDatabase, RemoteDatabaseError
from core.document_ai import AiError, FIELDS, parse_result


TOKEN = "test-only-token-" + "x" * 32


class CentralServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = Database(self.root / "server" / "records.db")
        self.server = DatabaseServer(("127.0.0.1", 0), self.database, TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.clients = [RemoteDatabase(self.url, TOKEN) for _ in range(3)]
        self.clients[0].save_telephely({"nev": "Site"}, "site")

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def permit(self, number="123"):
        return {
            "telephely_id": "site", "hatarozatszam": number, "engedely_nev": "Original",
            "hataly_tipus": "fixed", "lejarat": "2030-12-31", "felulvizsgalatok": [],
        }

    def test_three_clients_save_and_see_each_others_changes(self):
        def create(index):
            self.clients[index].save_permit(self.permit(str(index)), f"permit-{index}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            list(executor.map(create, range(3)))
        for client in self.clients:
            self.assertEqual(len(client.get_all_data()["engedelyek"]), 3)

    def test_concurrent_edit_has_one_winner_without_lost_updates(self):
        self.clients[0].save_permit(self.permit(), "permit")
        record = self.clients[0].get_all_data()["engedelyek"][0]

        def edit(index):
            updated = {**record, "engedely_nev": f"Editor {index}", "felulvizsgalatok": []}
            try:
                self.clients[index].save_permit(updated, "permit", updating=True)
            except ConflictError:
                return False
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            winners = list(executor.map(edit, range(3)))
        self.assertEqual(winners.count(True), 1)
        stored = self.clients[0].get_all_data()["engedelyek"][0]
        self.assertEqual(stored["engedely_nev"], f"Editor {winners.index(True)}")

    def test_archive_delete_propagate_and_stale_bulk_is_atomic(self):
        client = self.clients[0]
        client.save_permit(self.permit(), "first")
        client.save_permit(self.permit(), "second")
        records = client.get_all_data()["engedelyek"]
        client.change_permits([records[1]], "archivalt", 1)
        with self.assertRaises(ConflictError):
            self.clients[1].change_permits(records, "deleted", 1)
        current = self.clients[2].get_all_data()["engedelyek"]
        self.assertEqual(len(current), 2)
        self.assertEqual(sum(row["archivalt"] for row in current), 1)
        client.change_permits(current, "deleted", 1)
        self.assertEqual(self.clients[1].get_all_data()["engedelyek"], [])
        with self.assertRaises(ConflictError):
            client.save_permit({**records[0], "felulvizsgalatok": []}, records[0]["id"], True)

    def test_site_conflict_and_atomic_reference_check(self):
        original = self.clients[0].get_telephelyek()[0]
        self.clients[1].save_telephely({**original, "nev": "Renamed"}, "site", True)
        with self.assertRaises(ConflictError):
            self.clients[2].save_telephely({**original, "nev": "Stale"}, "site", True)
        current = self.clients[0].get_telephelyek()[0]
        self.clients[0].save_permit(self.permit(), "permit")
        with self.assertRaises(ConflictError):
            self.clients[0].delete_telephely("site", current["revision"])
        self.clients[0].save_telephely({"nev": "Empty"}, "empty")
        empty = next(row for row in self.clients[1].get_telephelyek() if row["id"] == "empty")
        self.clients[1].delete_telephely("empty", empty["revision"])
        with self.assertRaises(ConflictError):
            self.clients[2].save_permit({**self.permit(), "telephely_id": "empty"}, "orphan")

    def test_backups_are_server_side_and_remote_restore_is_not_exposed(self):
        self.clients[0].save_permit(self.permit(), "permit")
        backup = self.clients[0].make_backup()
        self.assertIn(backup, self.clients[1].list_backups())
        self.assertFalse(backup.is_absolute())
        with sqlite3.connect(self.root / "server" / backup) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM engedelyek").fetchone()[0], 1)
        with self.assertRaises(RemoteDatabaseError):
            self.clients[0]._call("restore_backup", backup_path=str(backup))
        with self.assertRaises(RemoteDatabaseError):
            self.clients[0]._call("get_connection")

    def test_authentication_required_and_nonlocal_http_rejected(self):
        with self.assertRaises(RemoteDatabaseError):
            RemoteDatabase(self.url, "wrong-" + "x" * 32)
        with self.assertRaises(ValueError):
            RemoteDatabase("http://192.0.2.1:8765", TOKEN)
        with self.assertRaises(ValueError):
            RemoteDatabase(self.url, "")

    def test_invalid_parameters_and_oversized_request_are_rejected(self):
        for method, params in (
            ("change_permits", {"records": [], "field": "archivalt", "value": 1}),
            ("make_backup", {"keep": 0}),
            ("save_permit", {"data": self.permit(), "entity_id": "../escape"}),
        ):
            with self.subTest(method=method), self.assertRaises(RemoteDatabaseError):
                self.clients[0]._call(method, **params)
        request = Request(
            self.url + "/api", data=b"{}", method="POST",
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Length": str(MAX_REQUEST_BYTES + 1)},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=2)
        self.assertEqual(error.exception.code, 413)

    def test_network_failure_is_explicit_and_never_uses_local_storage(self):
        for failure in (OSError("offline"), IncompleteRead(b'{"res')):
            with self.subTest(failure=failure):
                with patch.object(self.clients[0]._opener, "open", side_effect=failure):
                    with self.assertRaisesRegex(RemoteDatabaseError, "bizonytalan"):
                        self.clients[0].save_permit(self.permit(), "permit")
        self.assertEqual(self.database.get_all_data()["engedelyek"], [])

    def test_ai_uses_authenticated_server_client_without_revealing_provider_key(self):
        data = {name: {"value": None, "status": "missing", "page": None, "evidence": None, "note": ""} for name in FIELDS}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-server-key-not-real"}):
            capabilities = self.clients[0].ai_capabilities()
            self.assertTrue(capabilities["enabled"])
            self.assertNotIn("test-server-key-not-real", str(capabilities))
            with patch.object(self.server.ai_client, "ai_extract", return_value=parse_result(data, 1)) as extract:
                result = self.clients[1].ai_extract(["synthetic-mocked-image"], True)
            extract.assert_called_once_with(images=["synthetic-mocked-image"], consent=True)
            self.assertEqual(result.subject.status, "missing")
        self.assertEqual(self.database.get_all_data()["engedelyek"], [])

    def test_ai_service_requires_consent_and_does_not_block_database_operations(self):
        with patch("core.document_ai.build_opener") as provider:
            with self.assertRaises(AiError):
                self.clients[0]._call("ai_extract", images=["synthetic"], consent=False)
            self.server.ai_slots.acquire()
            self.server.ai_slots.acquire()
            try:
                with self.assertRaisesRegex(AiError, "folyamatban"):
                    self.clients[0].ai_extract(["synthetic"], True)
                self.assertEqual(len(self.clients[1].get_telephelyek()), 1)
            finally:
                self.server.ai_slots.release()
                self.server.ai_slots.release()
            provider.assert_not_called()

    def test_ai_network_error_does_not_claim_a_database_write_or_retry(self):
        with patch.object(self.clients[0]._opener, "open", side_effect=OSError("offline")) as request:
            with self.assertRaisesRegex(AiError, "adatlap nem változott"):
                self.clients[0].ai_extract(["synthetic"], True)
            self.assertEqual(request.call_count, 1)

    def test_window_edits_through_remote_api_and_keeps_last_snapshot_on_failure(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        from gui.main_window import MainWindow, BackupRestoreDialog

        app = QApplication.instance() or QApplication([])
        window = MainWindow(self.clients[0], {"attachments": self.root / "shared"})
        self.addCleanup(window.deleteLater)
        for timer in window.findChildren(QTimer):
            timer.stop()
        window._persist_permit(self.permit(), "permit", "ignored-client-time")
        window.refresh_data()
        self.assertEqual(len(window.data["engedelyek"]), 1)
        record = window.data["engedelyek"][0]
        with patch("gui.main_window.confirm", return_value=True):
            window.arc_eng(record)
        self.assertEqual(window.data["engedelyek"][0]["archivalt"], 1)
        with patch.object(self.clients[0], "get_all_data", side_effect=RemoteDatabaseError("offline")):
            window.refresh_data()
        self.assertEqual(len(window.data["engedelyek"]), 1)
        self.assertIn("elavult", window.statusBar().currentMessage())
        dialog = BackupRestoreDialog(self.clients[0], window)
        self.addCleanup(dialog.deleteLater)
        self.assertFalse(dialog.btn_restore.isEnabled())
        app.processEvents()


class TLSTransportTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL needed to generate a disposable test certificate")
    def test_verified_tls_with_explicit_ca_and_rejection_without_trust(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            certificate, key = root / "certificate.pem", root / "key.pem"
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "1",
                "-nodes", "-keyout", str(key), "-out", str(certificate),
                "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            database = Database(root / "records.db")
            server = DatabaseServer(("127.0.0.1", 0), database, TOKEN)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                url = f"https://127.0.0.1:{server.server_port}"
                with self.assertRaises(RemoteDatabaseError):
                    RemoteDatabase(url, TOKEN)
                client = RemoteDatabase(url, TOKEN, str(certificate))
                client.save_telephely({"nev": "TLS site"}, "tls-site")
                self.assertEqual(client.get_telephelyek()[0]["nev"], "TLS site")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
