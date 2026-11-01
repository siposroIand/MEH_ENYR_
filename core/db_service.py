"""Small TLS service: only this process opens the server-local SQLite database."""
from __future__ import annotations

import argparse
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import sqlite3
import ssl

from core.db import ConflictError, Database

logger = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 2 * 1024 * 1024
METHODS = {
    "get_all_data", "get_telephelyek", "save_permit", "change_permits",
    "save_telephely", "delete_telephely", "list_backups", "make_backup", "log_event",
}


def _validate_params(method: str, params: dict) -> None:
    if method in ("save_permit", "save_telephely"):
        if not isinstance(params.get("data"), dict) or type(params.get("updating", False)) is not bool:
            raise ValueError("Érvénytelen adatlap.")
    if method in ("save_permit", "save_telephely", "delete_telephely"):
        entity_id = params.get("entity_id")
        if (not isinstance(entity_id, str) or not entity_id or len(entity_id) > 100
                or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in entity_id)):
            raise ValueError("Érvénytelen azonosító.")
    if method == "change_permits":
        records = params.get("records")
        if not isinstance(records, list) or not records or len(records) > 10000:
            raise ValueError("Érvénytelen kijelölés.")
        if any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
               or not isinstance(row.get("revision", ""), str) for row in records):
            raise ValueError("Érvénytelen tételverzió.")
    if method in ("save_permit", "save_telephely"):
        if not isinstance(params["data"].get("revision", ""), str):
            raise ValueError("Érvénytelen tételverzió.")
    if method == "delete_telephely" and not isinstance(params.get("revision"), str):
        raise ValueError("Hiányzó tételverzió.")
    if method == "list_backups":
        limit = params.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Érvénytelen listahossz.")
    if method == "make_backup":
        keep = params.get("keep", 20)
        if type(keep) is not int or not 1 <= keep <= 1000:
            raise ValueError("Érvénytelen mentésszám.")


class DatabaseServer(ThreadingHTTPServer):
    daemon_threads = False

    def __init__(self, address, database: Database, token: str):
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("A MEH_SERVER_TOKEN legalább 32 karakteres, szóköz nélküli ASCII-kulcs legyen.")
        self.database = database
        self.auth = ("Bearer " + token).encode("ascii")
        super().__init__(address, DatabaseHandler)


class DatabaseHandler(BaseHTTPRequestHandler):
    server: DatabaseServer

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def _reply(self, status: int, value: dict):
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api":
            self._reply(404, {"error": "Ismeretlen végpont."})
            return
        if not compare_digest(self.headers.get("Authorization", "").encode("utf-8"), self.server.auth):
            self._reply(401, {"error": "Hiányzó vagy hibás hozzáférési kulcs."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_REQUEST_BYTES:
                self._reply(413, {"error": "Érvénytelen kérésméret."})
                return
            request = json.loads(self.rfile.read(size))
            if not isinstance(request, dict):
                raise ValueError("Érvénytelen kérés.")
            method, params = request.get("method"), request.get("params", {})
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("Érvénytelen művelet.")
            if method == "health":
                result = {"version": 1}
            elif method in METHODS:
                _validate_params(method, params)
                result = getattr(self.server.database, method)(**params)
                if method == "list_backups":
                    result = [path.name for path in result]
                elif method == "make_backup" and result is not None:
                    result = result.name
            else:
                raise ValueError("Nem engedélyezett művelet.")
        except ConflictError as exc:
            self._reply(409, {"error": str(exc)})
        except sqlite3.IntegrityError as exc:
            self._reply(409, {"error": str(exc)})
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            logger.warning("Rejected invalid API request: %s", exc)
            self._reply(400, {"error": "Érvénytelen kérés vagy hiányzó kötelező adat."})
        except Exception:
            logger.exception("Database service operation failed")
            self._reply(500, {"error": "Szerverhiba. Ellenőrizze a szolgáltatás naplóját; ne feltételezze, hogy a mentés sikerült."})
        else:
            self._reply(200, {"result": result})

    def log_message(self, format, *args):
        logger.info("%s - %s", self.client_address[0], format % args)


def main() -> None:
    parser = argparse.ArgumentParser(description="MEH központi adatbázis-szolgáltatás")
    parser.add_argument("--data-dir", type=Path, required=True, help="Helyi szerverkönyvtár, NEM hálózati meghajtó")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cert", type=Path, help="TLS szervertanúsítvány PEM formátumban")
    parser.add_argument("--key", type=Path, help="TLS privát kulcs PEM formátumban")
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key):
        parser.error("A --cert és --key együtt szükséges.")
    if args.host not in ("127.0.0.1", "localhost") and not args.cert:
        parser.error("Hálózati eléréshez --cert és --key szükséges; titkosítatlan HTTP csak helyi teszthez engedélyezett.")
    token = os.environ.get("MEH_SERVER_TOKEN", "")
    if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
        parser.error("Állítson be legalább 32 karakteres MEH_SERVER_TOKEN környezeti változót.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    context = None
    if args.cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.cert, args.key)
    database = Database(args.data_dir / "engedelyek.db", args.data_dir / "backup")
    with DatabaseServer((args.host, args.port), database, token) as server:
        if context:
            server.socket = context.wrap_socket(server.socket, server_side=True)
        logger.info("MEH service listening on %s:%s (%s)", args.host, args.port, "HTTPS" if context else "local HTTP")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Service stopped.")


if __name__ == "__main__":
    main()
