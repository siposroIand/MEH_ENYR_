import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from core.auth import (
    AuthenticationError, ConflictError, LoginThrottled, PermissionDenied,
    Session, ValidationError, hash_password, verify_password,
)
from core.attachments import (
    AttachmentPlan, MAX_ATTACHMENT_BYTES, safe_attachment_path, validate_filename,
)
from core.db import Database
from core.services import PermitService


PASSWORD = "correct horse battery staple"


class PasswordTests(unittest.TestCase):
    def test_hashes_are_versioned_salted_and_verified(self):
        first, second = hash_password(PASSWORD), hash_password(PASSWORD)
        self.assertTrue(first.startswith("v1$"))
        self.assertNotEqual(first, second)
        self.assertNotIn(PASSWORD, first)
        self.assertTrue(verify_password(PASSWORD, first))
        self.assertFalse(verify_password("incorrect password", first))
        self.assertFalse(verify_password(PASSWORD, "sha256$legacy"))
        self.assertFalse(verify_password(PASSWORD, first.replace("v1$", "v2$")))

    def test_pbkdf2_primary_and_malformed_hashes(self):
        encoded = hash_password(PASSWORD)
        self.assertIn("$pbkdf2_sha256$600000$", encoded)
        self.assertTrue(verify_password(PASSWORD, encoded))
        self.assertFalse(verify_password(PASSWORD, encoded.replace("$600000$", "$999999999999$")))
        for encoded in (None, "", "v1$scrypt$9999999$8$1$00$00", "v1$scrypt$16384$8$1$xx$xx"):
            self.assertFalse(verify_password(PASSWORD, encoded))
        with self.assertRaises(ValidationError):
            hash_password("short")
        with self.assertRaises(ValidationError):
            hash_password("a" * 1025)

    def test_scrypt_verifier_requires_strong_bounded_parameters(self):
        salt, expected = b"s" * 16, b"d" * 32
        encoded = f"v1$scrypt$131072$8$1${salt.hex()}${expected.hex()}"
        with patch("core.auth.hashlib.scrypt", return_value=expected) as scrypt:
            self.assertTrue(verify_password(PASSWORD, encoded))
            scrypt.assert_called_once_with(PASSWORD.encode(), salt=salt, n=131072,
                                           r=8, p=1, dklen=32, maxmem=256 * 1024 * 1024)
        self.assertFalse(verify_password(PASSWORD, encoded.replace("$131072$", "$16384$")))

    def test_verification_uses_constant_time_comparison(self):
        encoded = hash_password(PASSWORD)
        import hmac
        with patch("core.auth.hmac.compare_digest", wraps=hmac.compare_digest) as compare:
            self.assertTrue(verify_password(PASSWORD, encoded))
            compare.assert_called_once()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).parent / (".services-test-" + uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.db = Database(self.directory / "database.db", self.directory / "backups")
        self.root = self.directory / "attachments"
        self.admin = PermitService(self.db, attachments_root=self.root)
        self.admin.bootstrap_admin("Admin", PASSWORD)
        self.admin_id = self.admin.current_user["id"]
        self.site = self.admin.save_site({"nev": "Budapest", "ktj": "123", "cim": "Cím"})
        self.clerk_user = self.admin.save_user({
            "username": "Clerk", "password": PASSWORD, "role": "clerk", "display_name": "Ügyintéző",
        })
        self.viewer_user = self.admin.save_user({
            "username": "Viewer", "password": PASSWORD, "role": "viewer",
        })
        self.clerk = PermitService(self.db, attachments_root=self.root)
        self.clerk.login("clerk", PASSWORD)
        self.viewer = PermitService(self.db, attachments_root=self.root)
        self.viewer.login("VIEWER", PASSWORD)

    def permit(self, service=None, **changes):
        values = {
            "telephely_id": self.site["id"], "engedely_nev": "Próba",
            "kategoria": "Levegőtisztaság-védelem",
            "hataly_tipus": "fixed", "lejarat": "2030-12-31",
            "felulvizsgalatok": [{"type": "before_expiry", "days": 0, "note": "Határnap"}],
        }
        values.update(changes)
        return (service or self.admin).save_permit(values)

    def audit_count(self):
        with self.db.get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]

    def source(self, name="document.pdf", content=b"original content"):
        path = self.directory / name
        path.write_bytes(content)
        return path

    def test_bootstrap_only_empty_and_session_is_opaque(self):
        with self.assertRaises(PermissionDenied):
            self.admin.bootstrap_admin("Other", PASSWORD)
        self.assertFalse(self.admin.needs_bootstrap())
        self.assertIsInstance(self.admin.session, Session)
        with self.db.get_connection() as conn:
            stored = conn.execute("SELECT token_hash FROM auth_sessions WHERE user_id=?", (self.admin_id,)).fetchone()[0]
        self.assertNotEqual(stored, self.admin.session.token)
        self.assertNotIn(self.admin.session.token, repr(self.admin.session))
        self.assertEqual(stored, hashlib.sha256(self.admin.session.token.encode()).hexdigest())

    def test_simultaneous_bootstrap_creates_exactly_one_admin(self):
        db = Database(self.directory / "race.db", self.directory / "race-backups")
        services = [PermitService(db), PermitService(db)]
        barrier = threading.Barrier(2)
        results = []

        def bootstrap(service):
            barrier.wait()
            try:
                service.bootstrap_admin("Admin", PASSWORD)
                results.append("ok")
            except PermissionDenied:
                results.append("denied")

        threads = [threading.Thread(target=bootstrap, args=(service,)) for service in services]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ["ok", "denied"])
        with db.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_persistent_bounded_login_throttle(self):
        for _ in range(5):
            with self.assertRaises(AuthenticationError):
                self.clerk.login("cLeRk", "incorrect password")
        fresh = PermitService(self.db)
        with self.assertRaises(LoginThrottled):
            fresh.login("CLERK", PASSWORD)
        with self.db.get_connection() as conn:
            self.assertEqual(conn.execute("SELECT failures FROM login_throttle WHERE username='clerk'").fetchone()[0], 5)
        with patch("core.auth.time.time", return_value=10000000000):
            fresh.login("CLERK", PASSWORD)
        self.assertEqual(fresh.current_user["id"], self.clerk_user["id"])

    def test_unknown_user_throttle_and_inactive_login(self):
        for _ in range(5):
            with self.assertRaises(AuthenticationError):
                self.admin.login("unknown", PASSWORD)
        with self.assertRaises(LoginThrottled):
            self.admin.login("UNKNOWN", PASSWORD)
        self.admin.deactivate_user(self.viewer_user["id"])
        with self.assertRaises(AuthenticationError):
            self.viewer.login("viewer", PASSWORD)

    def test_logout_revokes_shared_session_and_forged_session_denied(self):
        clone = PermitService(self.db, self.clerk.session)
        self.clerk.logout()
        with self.assertRaises(AuthenticationError):
            clone.snapshot()
        clone.session = Session(self.admin_id, 1, "forged-token")
        with self.assertRaises(AuthenticationError):
            clone.snapshot()

    def test_logout_clears_local_session_even_when_database_unavailable(self):
        with patch.object(self.db, "get_connection", side_effect=sqlite3.OperationalError("unavailable")):
            with self.assertRaises(sqlite3.OperationalError):
                self.clerk.logout()
        self.assertIsNone(self.clerk.session)

    def test_password_change_revokes_every_session(self):
        clone = PermitService(self.db, self.clerk.session)
        with self.assertRaises(AuthenticationError):
            self.clerk.change_password("wrong", "new valid password")
        self.clerk.change_password(PASSWORD, "new valid password")
        for service in (clone, self.clerk):
            with self.assertRaises(AuthenticationError):
                service.snapshot()
        with self.assertRaises(AuthenticationError):
            self.clerk.login("clerk", PASSWORD)
        self.clerk.login("clerk", "new valid password")

    def test_password_and_profile_change_are_atomic(self):
        with self.assertRaises(AuthenticationError):
            self.clerk.change_password("wrong", "new valid password", display_name="Must not change")
        self.assertEqual(self.clerk.current_user["display_name"], "Ügyintéző")
        self.clerk.change_password(PASSWORD, "new valid password", display_name=" Changed together ")
        self.assertIsNone(self.clerk.session)
        self.clerk.login("clerk", "new valid password")
        self.assertEqual(self.clerk.current_user["display_name"], "Changed together")

    def test_admin_reset_deactivation_and_role_change_revoke_sessions(self):
        self.admin.change_user_password(self.clerk_user["id"], "different valid password")
        with self.assertRaises(AuthenticationError):
            self.clerk.snapshot()
        self.clerk.login("clerk", "different valid password")
        self.admin.save_user({"id": self.clerk_user["id"], "role": "viewer"})
        with self.assertRaises(AuthenticationError):
            self.clerk.snapshot()
        self.clerk.login("clerk", "different valid password")
        with self.assertRaises(PermissionDenied):
            self.permit(self.clerk)
        self.admin.deactivate_user(self.clerk_user["id"])
        with self.assertRaises(AuthenticationError):
            self.clerk.snapshot()

    def test_last_active_admin_and_retained_users(self):
        for changes in ({"active": 0}, {"role": "viewer"}, {"role": "clerk"}):
            with self.assertRaises(ValidationError):
                self.admin.save_user(dict(changes, id=self.admin_id))
        second = self.admin.save_user({"username": "Second", "password": PASSWORD, "role": "admin"})
        other = PermitService(self.db)
        other.login("Second", PASSWORD)
        other.deactivate_user(self.admin_id)
        with self.assertRaises(AuthenticationError):
            self.admin.snapshot()
        self.assertEqual(len(other.list_users()), 4)
        with self.assertRaises(ValidationError):
            other.deactivate_user(second["id"])

    def test_duplicate_usernames_and_password_material_not_exposed(self):
        with self.assertRaises(ValidationError):
            self.admin.save_user({"username": "ADMIN", "password": PASSWORD})
        snapshot = self.viewer.snapshot()
        self.assertTrue(snapshot["users"])
        for user in snapshot["users"]:
            self.assertNotIn("password_hash", user)
            self.assertNotIn("password", user)
        with self.assertRaises(PermissionDenied):
            self.clerk.list_users()
        with self.assertRaises(PermissionDenied):
            self.clerk.save_user({"username": "Illegal", "password": PASSWORD})

    def test_creator_ownership_and_clerk_cannot_mutate_another_record(self):
        own = self.permit(self.clerk)
        other = self.permit()
        self.assertEqual(own["responsible_user_id"], self.clerk_user["id"])
        self.assertEqual(other["responsible_user_id"], self.admin_id)
        snapshot_records = {row["id"]: row for row in self.admin.snapshot()["permits"]}
        self.assertEqual(snapshot_records[own["id"]]["responsible_name"], "Ügyintéző")
        changed = self.clerk.save_permit({"id": own["id"], "megjegyzes": "Changed"}, own["version"])
        self.assertEqual(changed["version"], own["version"] + 1)
        with self.assertRaises(PermissionDenied):
            self.clerk.save_permit({"id": other["id"], "megjegyzes": "Forbidden"}, other["version"])
        with self.assertRaises(PermissionDenied):
            self.clerk.delete_permits([other["id"]], {other["id"]: other["version"]})
        with self.assertRaises(PermissionDenied):
            self.permit(self.clerk, responsible_user_id=self.admin_id)

    def test_viewer_read_only_and_site_admin_only(self):
        record = self.permit()
        self.assertEqual(len(self.viewer.snapshot()["permits"]), 1)
        with self.assertRaises(PermissionDenied):
            self.permit(self.viewer)
        with self.assertRaises(PermissionDenied):
            self.viewer.delete_permits([record["id"]], {record["id"]: record["version"]})
        for service in (self.viewer, self.clerk):
            with self.assertRaises(PermissionDenied):
                service.save_site({"nev": "Forbidden"})
            with self.assertRaises(PermissionDenied):
                service.delete_site(self.site["id"], self.site["version"])

    def test_optimistic_concurrency_and_missing_versions(self):
        record = self.permit()
        self.admin.save_permit({"id": record["id"], "megjegyzes": "one"}, record["version"])
        for version in (None, record["version"], "2", True):
            with self.assertRaises(ConflictError):
                self.admin.save_permit({"id": record["id"], "megjegyzes": "two"}, version)
        with self.assertRaises(ConflictError):
            self.admin.delete_permits([record["id"]])
        self.assertEqual(self.admin.snapshot()["permits"][0]["megjegyzes"], "one")

    def test_bulk_update_atomic_ownership_and_stale_version(self):
        own, other = self.permit(self.clerk), self.permit()
        versions = {own["id"]: own["version"], other["id"]: other["version"]}
        before = self.audit_count()
        with self.assertRaises(PermissionDenied):
            self.clerk.bulk_update([own["id"], other["id"]], {"archivalt": 1}, versions)
        self.assertEqual(self.audit_count(), before)
        self.assertTrue(all(not row["archivalt"] for row in self.admin.snapshot()["permits"]))
        versions[other["id"]] = 999
        with self.assertRaises(ConflictError):
            self.admin.bulk_update([own["id"], other["id"]], {"archivalt": 1}, versions)
        self.assertTrue(all(not row["archivalt"] for row in self.admin.snapshot()["permits"]))

    def test_assignment_admin_only_active_and_reassignment_revokes_owner_access(self):
        record = self.permit(self.clerk)
        with self.assertRaises(PermissionDenied):
            self.clerk.assign([record["id"]], self.admin_id, {record["id"]: record["version"]})
        with self.assertRaises(ValidationError):
            self.admin.assign([record["id"]], self.viewer_user["id"], {record["id"]: record["version"]})
        changed = self.admin.assign([record["id"]], self.admin_id, {record["id"]: record["version"]})[0]
        with self.assertRaises(PermissionDenied):
            self.clerk.save_permit({"id": record["id"], "archivalt": 1}, changed["version"])
        self.assertEqual(changed["responsible_user_id"], self.admin_id)

    def test_site_versions_and_no_deletion_with_soft_deleted_permit(self):
        changed = self.admin.save_site({"id": self.site["id"], "nev": "New"}, self.site["version"])
        with self.assertRaises(ConflictError):
            self.admin.save_site({"id": self.site["id"], "nev": "Stale"}, self.site["version"])
        record = self.permit()
        self.admin.delete_permits([record["id"]], {record["id"]: record["version"]})
        with self.assertRaises(ValidationError):
            self.admin.delete_site(self.site["id"], changed["version"])
        empty = self.admin.save_site({"nev": "Empty"})
        self.admin.delete_site(empty["id"], empty["version"])

    def test_validation_reviews_dates_and_no_injection(self):
        for changes in (
            {"kategoria": ""}, {"telephely_id": "missing"},
            {"lejarat": "2030-02-31"}, {"felulvizsgalatok": "invalid"},
            {"felulvizsgalatok": [{"type": "before_expiry", "days": -1}]},
            {"hataly_tipus": "indefinite"},
            {"felulvizsgalatok": [{"type": "date", "date": "invalid"}]},
            {"arbitrary_column = 1": "injection"},
        ):
            with self.assertRaises(ValidationError):
                self.permit(**changes)
        record = self.permit(felulvizsgalatok=[
            {"date": "2030-01-01", "note": "legacy"}, {"type": "before_expiry", "days": 5},
        ])
        self.assertEqual(len(json.loads(record["felulvizsgalatok"])), 2)
        self.assertEqual(json.loads(record["felulvizsgalatok"])[0]["type"], "before_expiry")
        changed = self.admin.save_permit({"id": record["id"], "felulvizsgalatok": []}, record["version"])
        self.assertEqual(json.loads(changed["felulvizsgalatok"]), [])

    def test_audit_failure_rolls_back_mutation(self):
        before = self.audit_count()
        with patch("core.services.audit", side_effect=sqlite3.OperationalError("audit failure")):
            with self.assertRaises(sqlite3.OperationalError):
                self.permit()
        self.assertEqual(self.admin.snapshot()["permits"], [])
        self.assertEqual(self.audit_count(), before)

    def test_backup_facade_requires_admin_and_online_restore_remains_disabled(self):
        self.assertIs(self.admin.auth, self.admin)
        for service in (self.clerk, self.viewer):
            for operation in (service.list_backups, service.create_backup,
                              lambda: service.restore_backup(self.directory / "unused")):
                with self.assertRaises(PermissionDenied):
                    operation()
        backup = self.admin.create_backup()
        self.assertTrue(backup.is_file())
        self.assertIn(backup, self.admin.list_backups())
        with sqlite3.connect(backup) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with self.assertRaises(ValidationError):
            self.admin.restore_backup(backup)

    def test_gui_convenience_aliases_preserve_versions_and_permissions(self):
        record = self.permit(self.clerk)
        record = self.clerk.save_permit({"megjegyzes": "keyword ID"},
                                       permit_id=record["id"], expected_version=record["version"])
        record = self.clerk.archive_permits(
            [record["id"]], True, {record["id"]: record["version"]})[0]
        self.assertEqual(record["archivalt"], 1)
        record = self.clerk.add_attachments(record["id"], [self.source()], record["version"])
        self.assertEqual(self.clerk.list_attachments(record["id"]), ["document.pdf"])
        record = self.admin.assign_permits(
            [record["id"]], self.admin_id, {record["id"]: record["version"]})[0]
        self.assertEqual(record["responsible_user_id"], self.admin_id)
        site = self.admin.save_site({"nev": "keyword site ID"}, site_id=self.site["id"],
                                    expected_version=self.site["version"])
        self.assertEqual(site["nev"], "keyword site ID")

    def test_auth_dialog_convenience_api_and_profile_updates(self):
        session = self.clerk.session
        user = self.clerk.update_profile(" Új név ")
        self.assertEqual(user["display_name"], "Új név")
        self.assertEqual(self.clerk.session, session)
        self.assertEqual(self.clerk.current_user["display_name"], "Új név")
        created = self.admin.create_user("NewUser", "New User", PASSWORD, "clerk")
        updated = self.admin.update_user(created["id"], "Updated User", "viewer", 0)
        self.assertEqual((updated["display_name"], updated["role"], updated["active"]),
                         ("Updated User", "viewer", 0))
        self.admin.reset_password(created["id"], "different valid password")
        with self.assertRaises(ValidationError):
            self.clerk.update_profile("")
        with self.assertRaises(PermissionDenied):
            self.clerk.create_user("Illegal", "Illegal", PASSWORD, "admin")
        fresh_db = Database(self.directory / "bootstrap-alias.db", self.directory / "alias-backups")
        auth = PermitService(fresh_db)
        auth.bootstrap("First", "Első admin", PASSWORD)
        self.assertEqual(auth.current_user["display_name"], "Első admin")

    def test_attachment_add_cancel_save_and_soft_delete(self):
        source = self.source()
        plan = self.clerk.attachment_plan()
        self.assertEqual(Path(plan._temporary_directory.name).parent, self.root.absolute() / ".staging")
        plan.add(source)
        self.assertEqual(plan.list_names(), ["document.pdf"])
        self.assertTrue(plan.path_for("document.pdf").is_file())
        plan.cancel()
        self.assertFalse(any(self.root.glob("*/document.pdf")))
        plan = self.clerk.attachment_plan()
        plan.add(source)
        record = self.clerk.save_permit({
            "telephely_id": self.site["id"], "engedely_nev": "With attachment",
            "kategoria": "Levegőtisztaság-védelem",
        }, attachment_plan=plan)
        destination = self.clerk.attachment_path(record["id"], "document.pdf")
        self.assertEqual(destination.read_bytes(), source.read_bytes())
        plan.cancel()
        self.assertTrue(destination.exists())
        self.clerk.delete_permits([record["id"]], {record["id"]: record["version"]})
        self.assertTrue(destination.exists())
        self.assertEqual(self.clerk.snapshot()["permits"], [])

    def test_attachment_permissions_and_stale_plan_no_files_changed(self):
        record = self.permit(self.clerk)
        with self.assertRaises(PermissionDenied):
            self.viewer.attachment_plan(record["id"])
        plan = self.clerk.attachment_plan(record["id"])
        plan.add(self.source())
        self.admin.save_permit({"id": record["id"], "megjegyzes": "new"}, record["version"])
        with self.assertRaises(ConflictError):
            self.clerk.save_attachments(record["id"], plan, record["version"])
        self.assertEqual(self.clerk.list_attachments(record["id"]), [])
        plan.cancel()

    def test_attachment_guard_revalidates_open_dialog_and_readonly_mode(self):
        record = self.permit(self.clerk)
        plan = self.clerk.attachment_plan(record["id"])
        plan.add(self.source())
        record = self.clerk.save_attachments(record["id"], plan, record["version"])
        readonly = self.viewer.attachment_plan(record["id"], readonly=True)
        self.addCleanup(readonly.cancel)
        self.assertEqual(readonly.list_names(), ["document.pdf"])
        with self.assertRaises(PermissionDenied):
            readonly.add(self.source("new.txt"))
        with self.assertRaises(PermissionDenied):
            readonly.delete("document.pdf")
        self.admin.deactivate_user(self.viewer_user["id"])
        with self.assertRaises(AuthenticationError):
            readonly.path_for("document.pdf")
        owned = self.clerk.attachment_plan(record["id"])
        self.addCleanup(owned.cancel)
        self.admin.assign([record["id"]], self.admin_id, {record["id"]: record["version"]})
        with self.assertRaises(PermissionDenied):
            owned.delete("document.pdf")

    def test_attachment_failure_compensates_added_files_and_restores_originals(self):
        record = self.permit(self.clerk)
        plan = self.clerk.attachment_plan(record["id"])
        plan.add(self.source())
        record = self.clerk.save_attachments(record["id"], plan, record["version"])
        destination = self.clerk.attachment_path(record["id"], "document.pdf")
        plan = self.clerk.attachment_plan(record["id"])
        plan.delete("document.pdf")
        plan.add(self.source("new.txt", b"new"))
        from core.services import audit

        def fail_attachment_audit(conn, actor_id, action, *args, **kwargs):
            if action == "attachments_changed":
                raise sqlite3.OperationalError("injected failure")
            return audit(conn, actor_id, action, *args, **kwargs)

        before = self.audit_count()
        with patch("core.services.audit", side_effect=fail_attachment_audit):
            with self.assertRaises(sqlite3.OperationalError):
                self.clerk.save_attachments(record["id"], plan, record["version"])
        self.assertEqual(destination.read_bytes(), b"original content")
        self.assertFalse((destination.parent / "new.txt").exists())
        self.assertEqual(self.audit_count(), before)
        self.assertEqual(self.clerk.snapshot()["permits"][0]["version"], record["version"])
        plan.cancel()
        self.assertTrue(destination.exists())

    def test_attachment_remove_and_cancel_preserve_original_until_save(self):
        record = self.permit()
        plan = self.admin.attachment_plan(record["id"])
        plan.add(self.source())
        record = self.admin.save_attachments(record["id"], plan, record["version"])
        destination = self.admin.attachment_path(record["id"], "document.pdf")
        plan = self.admin.attachment_plan(record["id"])
        plan.delete("document.pdf")
        self.assertEqual(plan.list_names(), [])
        self.assertTrue(destination.exists())
        plan.cancel()
        self.assertTrue(destination.exists())
        plan = self.admin.attachment_plan(record["id"])
        plan.delete("document.pdf")
        self.admin.save_attachments(record["id"], plan, record["version"])
        self.assertFalse(destination.exists())

    def test_attachment_traversal_symlink_extension_and_size_policy(self):
        plan = self.admin.attachment_plan()
        self.addCleanup(plan.cancel)
        source = self.source()
        for name in ("../escape.pdf", r"..\escape.pdf", "/abs.pdf", ".hidden.pdf", "run.exe", "CON.pdf", "a.pdf "):
            with self.assertRaises(ValidationError):
                plan.add(source, filename=name)
        link = self.directory / "link.pdf"
        link.symlink_to(source)
        with self.assertRaises(ValidationError):
            plan.add(link)
        linked_directory = self.directory / "linked"
        linked_directory.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(ValidationError):
            plan.add(linked_directory / "document.pdf")
        with self.assertRaises(ValidationError):
            safe_attachment_path(self.root, "../escape", "document.pdf")
        oversized = self.directory / "oversized.pdf"
        with oversized.open("wb") as stream:
            stream.truncate(MAX_ATTACHMENT_BYTES + 1)
        with self.assertRaises(ValidationError):
            plan.add(oversized)
        executable = self.source("program.exe")
        with self.assertRaises(ValidationError):
            plan.add(executable, filename="disguised.pdf")
        self.assertEqual(plan.list_names(), [])

    def test_attachment_wrong_root_and_wrong_permit_rejected(self):
        record, other = self.permit(), self.permit()
        with AttachmentPlan(self.directory / "wrong-root") as plan:
            plan.add(self.source())
            with self.assertRaises(ValidationError):
                self.admin.save_attachments(record["id"], plan, record["version"])
        with self.admin.attachment_plan(other["id"]) as plan:
            plan.add(self.source())
            with self.assertRaises(ValidationError):
                self.admin.save_attachments(record["id"], plan, record["version"])
        self.assertEqual(self.admin.list_attachments(record["id"]), [])


if __name__ == "__main__":
    unittest.main()
