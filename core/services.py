"""Authorized, transactional application operations; GUI code must not write SQL."""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from core.auth import (
    AuthService, AuthenticationError, AuthorizationError, ConflictError,
    PermissionDenied, ServiceError, ValidationError, Session, ROLE_LABELS,
    audit, public_user, transaction,
)
from core.attachments import AttachmentPlan, safe_attachment_path
from utils.reviews import parse_reviews


PERMIT_FIELDS = frozenset({
    "telephely_id", "engedely_nev", "kategoria", "kiado_hatosag",
    "hatarozatszam", "modosito_hat", "letrejott", "hatarozatlan",
    "hatarozatlan_szoveg", "lejarat", "felulvizsgalat_nap",
    "felulvizsgalat_date", "felulvizsgalatok", "megjegyzes", "archivalt",
    "hulladek_nv_elokez", "hulladek_v_elokez", "hulladek_nv_gyujt",
    "hulladek_v_gyujt", "hataly_tipus", "responsible_user_id",
})


def _iso(value, field):
    if value in ("", None):
        return None
    try:
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except ValueError as exc:
        raise ValidationError(f"Érvénytelen dátum: {field}.") from exc
    return value


def normalize_reviews(value):
    parsed = parse_reviews({"felulvizsgalatok": value})
    if not parsed.valid:
        raise ValidationError("; ".join(parsed.errors))
    if len(parsed.entries) > 1000:
        raise ValidationError("Érvénytelen felülvizsgálati lista.")
    result, seen = [], set()
    for entry in parsed.entries:
        if not isinstance(entry, dict):
            raise ValidationError("Érvénytelen felülvizsgálati bejegyzés.")
        kind = entry.get("type") or ("date" if entry.get("date") else None)
        note = entry.get("note", "")
        if not isinstance(note, str) or len(note) > 10000:
            raise ValidationError("Érvénytelen felülvizsgálati megjegyzés.")
        if kind == "date":
            when = _iso(entry.get("date"), "felülvizsgálat")
            if not when:
                raise ValidationError("A felülvizsgálat dátuma kötelező.")
            cleaned, key = {"type": "date", "date": when, "note": note}, ("date", when)
        elif kind == "before_expiry":
            days = entry.get("days")
            if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 36500:
                raise ValidationError("A napok száma 0–36500 közötti egész szám legyen.")
            cleaned, key = {"type": kind, "days": days, "note": note}, (kind, days)
        else:
            raise ValidationError("Ismeretlen felülvizsgálati típus.")
        if key in seen:
            raise ValidationError("Ismétlődő felülvizsgálati bejegyzés.")
        seen.add(key)
        result.append(cleaned)
    return sorted(result, key=lambda x: (x["type"], x.get("date", ""), x.get("days", 0)))


class PermitService(AuthService):
    def __init__(self, db, session=None, attachments_root=None):
        super().__init__(db, session)
        self.auth = self
        self.attachments_root = Path(attachments_root) if attachments_root is not None else None
        self._warnings = []

    def snapshot(self):
        with transaction(self.db) as conn:
            user = public_user(self._current(conn))
            permits = [dict(row) for row in conn.execute(
                """SELECT p.*,COALESCE(NULLIF(u.display_name,''),u.username,'') AS responsible_name
                FROM engedelyek p LEFT JOIN users u ON u.id=p.responsible_user_id
                WHERE COALESCE(p.deleted,0)=0 ORDER BY p.id""")]
            sites = [dict(row) for row in conn.execute("SELECT * FROM telephelyek ORDER BY nev,id")]
            users = [public_user(row) for row in conn.execute("SELECT * FROM users ORDER BY username")]
            return {"permits": permits, "sites": sites, "users": users, "current_user": user,
                    "engedelyek": permits, "telephelyek": sites}

    @staticmethod
    def _may_write(user, record=None):
        if user["role"] == "admin":
            return
        if user["role"] != "clerk" or (record is not None and record["responsible_user_id"] != user["id"]):
            raise PermissionDenied("Csak saját ügyek módosíthatók ügyintézőként.")

    @staticmethod
    def _permit(conn, permit_id):
        row = conn.execute("SELECT * FROM engedelyek WHERE id=? AND COALESCE(deleted,0)=0",
                           (str(permit_id),)).fetchone()
        if row is None:
            raise ConflictError("Az engedélyt időközben törölték vagy nem található.")
        return dict(row)

    @staticmethod
    def _version(record, expected):
        if (isinstance(expected, bool) or not isinstance(expected, int)
                or expected < 1 or record["version"] != expected):
            raise ConflictError("Az adat időközben megváltozott. Frissítse a listát.")

    @staticmethod
    def _assignee(conn, user_id):
        if user_id is None:
            return
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None or not row["active"] or row["role"] not in {"admin", "clerk"}:
            raise ValidationError("Csak aktív ügyintéző vagy adminisztrátor jelölhető ki.")

    def _validate_permit(self, conn, values, *, validate_reviews=True):
        if not isinstance(values.get("kategoria"), str) or not values["kategoria"].strip():
            raise ValidationError("Az engedélytípus kiválasztása kötelező.")
        for field in PERMIT_FIELDS - {"hatarozatlan", "archivalt", "felulvizsgalat_nap", "felulvizsgalatok"}:
            value = values.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 100000):
                raise ValidationError(f"Érvénytelen mező: {field}.")
        for field in ("hatarozatlan", "archivalt"):
            if values.get(field) not in (0, 1, None, False, True):
                raise ValidationError(f"Érvénytelen logikai érték: {field}.")
            values[field] = int(bool(values.get(field)))
        for field in ("letrejott", "lejarat", "felulvizsgalat_date"):
            if field == "felulvizsgalat_date" and not validate_reviews:
                continue
            values[field] = _iso(values.get(field), field)
        days = values.get("felulvizsgalat_nap")
        if not validate_reviews:
            pass
        elif days not in ("", None):
            if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 36500:
                raise ValidationError("Érvénytelen felülvizsgálati napszám.")
        else:
            values["felulvizsgalat_nap"] = None
        kind = values.get("hataly_tipus") or ("indefinite" if values["hatarozatlan"] else
                                             "fixed" if values.get("lejarat") else "until_modified")
        if kind not in {"fixed", "indefinite", "until_modified"}:
            raise ValidationError("Érvénytelen hatálytípus.")
        values["hataly_tipus"] = kind
        if kind == "fixed" and not values.get("lejarat"):
            raise ValidationError("Határozott hatály esetén lejárati dátum szükséges.")
        if kind == "indefinite":
            values["hatarozatlan"], values["lejarat"] = 1, "9999-12-31"
        elif kind == "until_modified":
            values["hatarozatlan"], values["lejarat"] = 0, None
        else:
            values["hatarozatlan"] = 0
        if validate_reviews:
            parsed = parse_reviews(values)
            if not parsed.valid:
                raise ValidationError("; ".join(parsed.errors))
            reviews = normalize_reviews(list(parsed.entries))
            if kind != "fixed" and any(item["type"] == "before_expiry" for item in reviews):
                raise ValidationError("Lejárat előtti felülvizsgálat csak határozott hatályhoz adható.")
            values["felulvizsgalatok"] = json.dumps(reviews, ensure_ascii=False)
        site_id = values.get("telephely_id")
        if not site_id or conn.execute("SELECT 1 FROM telephelyek WHERE id=?", (site_id,)).fetchone() is None:
            raise ValidationError("Létező telephely kiválasztása szükséges.")
        return values

    @staticmethod
    def _changes(values):
        values = dict(values)
        extras = set(values) - PERMIT_FIELDS - {"id", "version", "last_modified", "deleted", "responsible_name"}
        if extras:
            raise ValidationError("Ismeretlen engedélymező: " + ", ".join(sorted(extras)))
        if values.get("deleted") not in (None, 0, False):
            raise ValidationError("Törléshez a törlési műveletet használja.")
        return {key: value for key, value in values.items() if key in PERMIT_FIELDS}

    def _save_permit(self, conn, user, values, expected_version):
        changes = self._changes(values)
        permit_id = values.get("id")
        old = self._permit(conn, permit_id) if permit_id else None
        self._may_write(user, old)
        if old is not None:
            self._version(old, expected_version if expected_version is not None else values.get("version"))
            before = {key: old.get(key) for key in PERMIT_FIELDS}
            new = dict(before)
            new.update(changes)
            if user["role"] != "admin" and new.get("responsible_user_id") != old["responsible_user_id"]:
                raise PermissionDenied("Az ügy felelősét csak adminisztrátor módosíthatja.")
        else:
            permit_id = str(uuid.uuid4())
            before = None
            new = dict(changes)
            if user["role"] != "admin" and new.get("responsible_user_id") not in (None, user["id"]):
                raise PermissionDenied("Csak saját ügy hozható létre.")
            if new.get("responsible_user_id") is None:
                new["responsible_user_id"] = user["id"]
        if old is None or new.get("responsible_user_id") != old["responsible_user_id"]:
            self._assignee(conn, new.get("responsible_user_id"))
        review_fields = {"felulvizsgalatok", "felulvizsgalat_nap", "felulvizsgalat_date",
                         "lejarat", "hataly_tipus", "hatarozatlan"}
        validate_reviews = old is None or any(
            key in changes and changes[key] != old.get(key) for key in review_fields)
        new = self._validate_permit(conn, new, validate_reviews=validate_reviews)
        new["last_modified"] = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        new["version"] = old["version"] + 1 if old else 1
        columns = sorted(new)
        if old:
            sql = ",".join(f"{key}=?" for key in columns)
            cursor = conn.execute(f"UPDATE engedelyek SET {sql} WHERE id=? AND version=?",
                                  [new[key] for key in columns] + [permit_id, old["version"]])
            if cursor.rowcount != 1:
                raise ConflictError("Az engedély időközben megváltozott.")
        else:
            conn.execute(f"INSERT INTO engedelyek(id,{','.join(columns)}) VALUES({','.join('?' for _ in range(len(columns)+1))})",
                         [permit_id] + [new[key] for key in columns])
        audit(conn, user["id"], "permit_updated" if old else "permit_created", permit_id,
              {"before": before, "after": new})
        return self._permit(conn, permit_id)

    def save_permit(self, values, expected_version=None, attachment_plan=None, *, permit_id=None):
        values = dict(values)
        if permit_id is not None:
            if values.get("id") not in (None, permit_id):
                raise ValidationError("Ellentmondó engedélyazonosítók.")
            values["id"] = permit_id
        if attachment_plan is not None:
            self._check_plan(attachment_plan)
        try:
            with transaction(self.db) as conn:
                user = self._current(conn)
                saved = self._save_permit(conn, user, values, expected_version)
                if attachment_plan is not None and attachment_plan.has_changes:
                    attachment_plan.prepare(saved["id"])
                    audit(conn, user["id"], "attachments_changed", saved["id"])
        except BaseException:
            if attachment_plan is not None and attachment_plan._prepared:
                attachment_plan.rollback()
            raise
        if attachment_plan is not None:
            attachment_plan.finalize()
            self._warnings.extend(attachment_plan.cleanup_warnings)
        return saved

    @staticmethod
    def _ids(ids):
        ids = list(ids)
        if not ids or len(ids) > 10000 or len(set(ids)) != len(ids):
            raise ValidationError("Egyedi engedélyazonosítók szükségesek.")
        return ids

    @staticmethod
    def _expected(expected_versions, permit_id):
        if not isinstance(expected_versions, dict) or permit_id not in expected_versions:
            raise ConflictError("Minden engedélyhez az olvasott verzió szükséges.")
        return expected_versions[permit_id]

    def bulk_update(self, ids, changes, expected_versions):
        ids = self._ids(ids)
        if set(changes) - PERMIT_FIELDS:
            raise ValidationError("Érvénytelen tömeges módosítás.")
        with transaction(self.db) as conn:
            user = self._current(conn)
            return [self._save_permit(conn, user, dict(changes, id=permit_id),
                                      self._expected(expected_versions, permit_id)) for permit_id in ids]

    def assign(self, ids, user_id, expected_versions):
        ids = self._ids(ids)
        with transaction(self.db) as conn:
            user = self._current(conn, admin=True)
            self._assignee(conn, user_id)
            return [self._save_permit(conn, user, {"id": permit_id, "responsible_user_id": user_id},
                                      self._expected(expected_versions, permit_id)) for permit_id in ids]

    def delete_permits(self, ids, expected_versions=None):
        ids = self._ids(ids)
        with transaction(self.db) as conn:
            user = self._current(conn)
            for permit_id in ids:
                old = self._permit(conn, permit_id)
                self._may_write(user, old)
                self._version(old, self._expected(expected_versions, permit_id))
                conn.execute("""UPDATE engedelyek SET deleted=1,version=version+1,last_modified=?
                    WHERE id=? AND version=?""",
                    (datetime.now(timezone.utc).isoformat(timespec="microseconds"), permit_id, old["version"]))
                audit(conn, user["id"], "permit_deleted", permit_id, {"before": old})
        # Soft deletion intentionally preserves the permit's attachment directory.

    def save_site(self, values, expected_version=None, *, site_id=None):
        values = dict(values)
        if site_id is not None:
            if values.get("id") not in (None, site_id):
                raise ValidationError("Ellentmondó telephelyazonosítók.")
            values["id"] = site_id
        if set(values) - {"id", "nev", "ktj", "cim", "version"}:
            raise ValidationError("Ismeretlen telephelymező.")
        with transaction(self.db) as conn:
            user = self._current(conn, admin=True)
            site_id = values.get("id")
            old = conn.execute("SELECT * FROM telephelyek WHERE id=?", (site_id,)).fetchone() if site_id else None
            if site_id and old is None:
                raise ConflictError("A telephely nem található.")
            if old:
                self._version(old, expected_version if expected_version is not None else values.get("version"))
            new = {key: values.get(key, old[key] if old else "") for key in ("nev", "ktj", "cim")}
            if not isinstance(new["nev"], str) or not new["nev"].strip():
                raise ValidationError("A telephely neve kötelező.")
            if any(value is not None and (not isinstance(value, str) or len(value) > 10000)
                   for value in new.values()):
                raise ValidationError("Érvénytelen telephelyadat.")
            site_id = site_id or str(uuid.uuid4())
            if old:
                conn.execute("UPDATE telephelyek SET nev=?,ktj=?,cim=?,version=version+1 WHERE id=?",
                             (new["nev"], new["ktj"], new["cim"], site_id))
            else:
                conn.execute("INSERT INTO telephelyek(id,nev,ktj,cim,version) VALUES(?,?,?,?,1)",
                             (site_id, new["nev"], new["ktj"], new["cim"]))
            audit(conn, user["id"], "site_updated" if old else "site_created", site_id,
                  {"before": dict(old) if old else None, "after": new})
            return dict(conn.execute("SELECT * FROM telephelyek WHERE id=?", (site_id,)).fetchone())

    def delete_site(self, site_id, expected_version=None):
        with transaction(self.db) as conn:
            user = self._current(conn, admin=True)
            old = conn.execute("SELECT * FROM telephelyek WHERE id=?", (site_id,)).fetchone()
            if old is None:
                raise ConflictError("A telephely nem található.")
            self._version(old, expected_version)
            if conn.execute("SELECT 1 FROM engedelyek WHERE telephely_id=? LIMIT 1", (site_id,)).fetchone():
                raise ValidationError("Engedélyhez kapcsolt telephely nem törölhető (archivált/törölt ügy sem).")
            conn.execute("DELETE FROM telephelyek WHERE id=?", (site_id,))
            audit(conn, user["id"], "site_deleted", site_id, {"before": dict(old)})

    def _check_plan(self, plan):
        if (not isinstance(plan, AttachmentPlan) or self.attachments_root is None
                or plan.root.absolute() != self.attachments_root.absolute()):
            raise ValidationError("Érvénytelen csatolmányterv vagy gyökérkönyvtár.")

    def attachment_plan(self, permit_id=None, readonly=False):
        with transaction(self.db) as conn:
            user = self._current(conn)
            record = self._permit(conn, permit_id) if permit_id else None
            if not readonly:
                self._may_write(user, record)
            if self.attachments_root is None:
                raise ValidationError("Nincs beállítva csatolmánykönyvtár.")

        def guard(write=False):
            with transaction(self.db) as conn:
                user = self._current(conn)
                record = self._permit(conn, permit_id) if permit_id else None
                if write:
                    if readonly:
                        raise PermissionDenied("A csatolmányterv csak olvasható.")
                    self._may_write(user, record)

        return AttachmentPlan(self.attachments_root, permit_id,
                              read_guard=guard, write_guard=lambda: guard(write=True))

    def list_attachments(self, permit_id):
        with transaction(self.db) as conn:
            self._current(conn)
            self._permit(conn, permit_id)
            if self.attachments_root is None:
                return []
            with AttachmentPlan(self.attachments_root, permit_id) as plan:
                return plan.list_names()

    def attachment_path(self, permit_id, filename):
        with transaction(self.db) as conn:
            self._current(conn)
            self._permit(conn, permit_id)
            if self.attachments_root is None:
                raise ValidationError("Nincs beállítva csatolmánykönyvtár.")
            path = safe_attachment_path(self.attachments_root, permit_id, filename)
            if not path.is_file():
                raise ValidationError("A csatolmány nem található.")
            return path

    def save_attachments(self, permit_id, attachment_plan, expected_version):
        return self.save_permit({"id": permit_id}, expected_version, attachment_plan)

    def add_attachments(self, permit_id, paths, expected_version):
        with self.attachment_plan(permit_id) as plan:
            for path in paths:
                plan.add(path)
            return self.save_attachments(permit_id, plan, expected_version)

    def archive_permits(self, ids, archived, expected_versions):
        return self.bulk_update(ids, {"archivalt": archived}, expected_versions)

    assign_permits = assign

    def list_backups(self):
        with transaction(self.db) as conn:
            self._current(conn, admin=True)
            return self.db.list_backups()

    def create_backup(self):
        with transaction(self.db) as conn:
            user = self._current(conn, admin=True)
            path = self.db.make_backup(reason="manual")
            if path is None:
                raise ServiceError("A biztonsági mentés nem készült el.")
            audit(conn, user["id"], "backup_created", details={"filename": path.name})
            return path

    def restore_backup(self, backup_path):
        with transaction(self.db) as conn:
            self._current(conn, admin=True)
            try:
                return self.db.restore_backup(backup_path)
            except RuntimeError as exc:
                raise ValidationError(str(exc)) from exc

    def take_warnings(self):
        with transaction(self.db) as conn:
            self._current(conn)
            warnings, self._warnings = self._warnings, []
            return warnings


Service = PermitService
