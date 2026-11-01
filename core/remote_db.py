"""Explicit database operations over the authenticated central service."""
from __future__ import annotations

import json
from http.client import HTTPException
from pathlib import Path
import sqlite3
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler

from core.db import ConflictError
from core.document_ai import AI_TIMEOUT, AiError, parse_result


class RemoteDatabaseError(sqlite3.DatabaseError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RemoteDatabase:
    is_remote = True

    def __init__(self, url: str, token: str, ca_file: str | None = None):
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Érvénytelen MEH_SERVER_URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("A szerver címe csak protokollt, gépnevet és portot tartalmazhat.")
        if parsed.scheme != "https" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Távoli géphez HTTPS-kapcsolat szükséges.")
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("A MEH_SERVER_TOKEN legalább 32 karakteres, szóköz nélküli ASCII-kulcs legyen.")
        self.url = url.rstrip("/") + "/api"
        self._token = token
        context = ssl.create_default_context(cafile=ca_file or None)
        self._opener = build_opener(_NoRedirect(), HTTPSHandler(context=context))
        if self._call("health") != {"version": 1}:
            raise RemoteDatabaseError("A központi szolgáltatás verziója nem támogatott.")

    def _call(self, method: str, **params):
        is_ai = method in ("ai_capabilities", "ai_extract")
        request = Request(
            self.url.removesuffix("/api") + "/ai" if is_ai else self.url,
            data=json.dumps({"method": method, "params": params}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=AI_TIMEOUT + 30 if method == "ai_extract" else 15) as response:
                result = json.load(response)
            return result["result"]
        except HTTPError as exc:
            try:
                message = json.load(exc)["error"]
            except (ValueError, KeyError, TypeError):
                message = f"A központi szolgáltatás hibát jelzett (HTTP {exc.code})."
            if exc.code == 409:
                raise ConflictError(message) from exc
            if is_ai:
                raise AiError(message) from None
            raise RemoteDatabaseError(message) from exc
        except (URLError, OSError, HTTPException, ValueError, KeyError, TypeError) as exc:
            if is_ai:
                raise AiError(
                    "Az AI-szolgáltatás válasza nem érkezett meg. Nincs automatikus újraküldés; "
                    "a már elküldött OpenAI-kérés díja felmerülhet. Az adatlap nem változott."
                ) from None
            raise RemoteDatabaseError(
                "A központi szolgáltatás nem érhető el vagy érvénytelen választ adott. "
                "Mentés közbeni kapcsolathibánál a mentés eredménye bizonytalan: "
                "újrapróbálás előtt ellenőrizze az adatokat a lista frissítésével. "
                f"Helyi adatbázisra nem váltottunk át. Részlet: {exc}"
            ) from exc

    def get_all_data(self):
        return self._call("get_all_data")

    def get_telephelyek(self):
        return self._call("get_telephelyek")

    def save_permit(self, data: dict, entity_id: str, updating: bool = False):
        return self._call("save_permit", data=data, entity_id=entity_id, updating=updating)

    def change_permits(self, records: list[dict], field: str, value: int):
        versions = [{"id": row["id"], "revision": row.get("revision", "")} for row in records]
        return self._call("change_permits", records=versions, field=field, value=value)

    def save_telephely(self, data: dict, entity_id: str, updating: bool = False):
        return self._call("save_telephely", data=data, entity_id=entity_id, updating=updating)

    def delete_telephely(self, entity_id: str, revision: str):
        return self._call("delete_telephely", entity_id=entity_id, revision=revision)

    def list_backups(self, limit: int = 100):
        return [Path(name) for name in self._call("list_backups", limit=limit)]

    def make_backup(self, reason: str = "manual", keep: int = 20):
        name = self._call("make_backup", reason=reason, keep=keep)
        return Path(name) if name else None

    def log_event(self, note: str | None = None):
        return self._call("log_event", note=note)

    def ai_capabilities(self) -> dict:
        result = self._call("ai_capabilities")
        if (not isinstance(result, dict) or type(result.get("enabled")) is not bool
                or not isinstance(result.get("model"), str)):
            raise AiError("A szerver AI-beállításainak válasza hibás.")
        return result

    def ai_extract(self, images: list[str], consent: bool):
        if consent is not True:
            raise AiError("Külső AI-feldolgozáshoz külön jóváhagyás szükséges.")
        return parse_result(self._call("ai_extract", images=images, consent=True), len(images))
