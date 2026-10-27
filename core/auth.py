"""Local authentication. Database and attachment directories require trusted OS ACLs."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field


ROLE_LABELS = {"admin": "Adminisztrátor", "clerk": "Ügyintéző", "viewer": "Megtekintő"}
ROLES = frozenset(ROLE_LABELS)


class ServiceError(Exception):
    pass


class AuthenticationError(ServiceError):
    pass


class PermissionDenied(ServiceError):
    pass


class ConflictError(ServiceError):
    pass


class ValidationError(ServiceError, ValueError):
    pass


class LoginThrottled(AuthenticationError):
    pass


def _password_bytes(password):
    if not isinstance(password, str) or not 10 <= len(password) <= 1024:
        raise ValidationError("A jelszó hossza 10–1024 karakter legyen.")
    return password.encode("utf-8")


def hash_password(password: str) -> str:
    raw = _password_bytes(password)
    salt = secrets.token_bytes(16)
    try:
        digest = hashlib.scrypt(raw, salt=salt, n=16384, r=8, p=1, dklen=32)
        return f"v1$scrypt$16384$8$1${salt.hex()}${digest.hex()}"
    except (ValueError, AttributeError):
        digest = hashlib.pbkdf2_hmac("sha256", raw, salt, 600000)
        return f"v1$pbkdf2_sha256$600000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or len(password) > 1024:
        return False
    try:
        parts = encoded.split("$")
        if parts[:2] == ["v1", "scrypt"] and len(parts) == 7:
            _, _, n, r, p, salt, expected = parts
            if (n, r, p) != ("16384", "8", "1"):
                return False
            salt, expected = bytes.fromhex(salt), bytes.fromhex(expected)
            if len(salt) != 16 or len(expected) != 32:
                return False
            actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
        elif parts[:2] == ["v1", "pbkdf2_sha256"] and len(parts) == 5:
            _, _, iterations, salt, expected = parts
            if iterations != "600000":
                return False
            salt, expected = bytes.fromhex(salt), bytes.fromhex(expected)
            if len(salt) != 16 or len(expected) != 32:
                return False
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600000)
        else:
            return False
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return False


@dataclass(frozen=True)
class Session:
    user_id: str
    auth_version: int
    token: str = field(repr=False)


@contextmanager
def transaction(db):
    with db.get_connection(immediate=True) as conn:
        yield conn


def public_user(row):
    return {key: row[key] for key in ("id", "username", "display_name", "role", "active", "auth_version")}


def audit(conn, actor_id, action, entity_id=None, details=None):
    import json
    conn.execute(
        "INSERT INTO audit_log(ts,actor_id,action,entity_id,details) VALUES(?,?,?,?,?)",
        (time.time(), actor_id, action, entity_id, json.dumps(details or {}, ensure_ascii=False)),
    )


class AuthService:
    SESSION_SECONDS = 12 * 60 * 60
    THROTTLE_WINDOW = 300
    MAX_FAILURES = 5

    def __init__(self, db, session=None):
        self.db = db
        self.session = session
        with transaction(db) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS auth_sessions(
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                auth_version INTEGER NOT NULL, expires_at REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS login_throttle(
                username TEXT PRIMARY KEY, failures INTEGER NOT NULL, window_start REAL NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS audit_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                actor_id TEXT, action TEXT NOT NULL, entity_id TEXT, details TEXT NOT NULL)""")
        self._dummy_hash = hash_password(secrets.token_urlsafe(24))

    def needs_bootstrap(self):
        with transaction(self.db) as conn:
            return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None

    def _new_session(self, conn, user):
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (time.time(),))
        conn.execute("INSERT INTO auth_sessions VALUES(?,?,?,?)", (
            hashlib.sha256(token.encode()).hexdigest(), user["id"], user["auth_version"],
            time.time() + self.SESSION_SECONDS,
        ))
        return Session(user["id"], int(user["auth_version"]), token)

    def _current(self, conn, admin=False):
        session = self.session
        if not isinstance(session, Session):
            raise AuthenticationError("Bejelentkezés szükséges.")
        row = conn.execute("""SELECT u.* FROM users u JOIN auth_sessions s ON s.user_id=u.id
            WHERE s.token_hash=? AND s.expires_at>? AND s.auth_version=u.auth_version
            AND u.id=? AND u.auth_version=? AND u.active=1""", (
                hashlib.sha256(session.token.encode()).hexdigest(), time.time(),
                session.user_id, session.auth_version,
            )).fetchone()
        if row is None:
            raise AuthenticationError("A munkamenet lejárt vagy visszavonták.")
        if admin and row["role"] != "admin":
            raise PermissionDenied("Adminisztrátori jogosultság szükséges.")
        return row

    @property
    def current_user(self):
        with transaction(self.db) as conn:
            return public_user(self._current(conn))

    def bootstrap_admin(self, username, password, display_name=""):
        username = self._username(username)
        encoded = hash_password(password)
        with transaction(self.db) as conn:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise PermissionDenied("A kezdeti adminisztrátor már létrejött.")
            user_id = str(uuid.uuid4())
            conn.execute("""INSERT INTO users
                (id,username,display_name,password_hash,role,active,auth_version)
                VALUES(?,?,?,?, 'admin',1,1)""", (user_id, username, display_name or username, encoded))
            user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            session = self._new_session(conn, user)
            audit(conn, user_id, "bootstrap", user_id)
        self.session = session
        return session

    @staticmethod
    def _username(username):
        if not isinstance(username, str) or not username.strip() or len(username.strip()) > 128:
            raise ValidationError("Érvényes felhasználónév szükséges (legfeljebb 128 karakter).")
        return username.strip()

    def login(self, username, password):
        username = self._username(username)
        now = time.time()
        failure = False
        throttled = False
        with transaction(self.db) as conn:
            conn.execute("DELETE FROM login_throttle WHERE window_start<=?", (now - self.THROTTLE_WINDOW,))
            throttle = conn.execute("SELECT * FROM login_throttle WHERE username=?", (username.casefold(),)).fetchone()
            global_throttle = conn.execute("SELECT * FROM login_throttle WHERE username=''", ()).fetchone()
            throttled = bool((throttle and throttle["failures"] >= self.MAX_FAILURES) or
                             (global_throttle and global_throttle["failures"] >= 100))
            if not throttled:
                user = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
                valid = verify_password(password, user["password_hash"] if user else self._dummy_hash)
                if user is None or not valid or not user["active"]:
                    failure = True
                    for key in (username.casefold(), ""):
                        conn.execute("""INSERT INTO login_throttle VALUES(?,1,?)
                            ON CONFLICT(username) DO UPDATE SET failures=failures+1""", (key, now))
                    audit(conn, None, "login_failed")
                else:
                    conn.execute("DELETE FROM login_throttle WHERE username=?", (username.casefold(),))
                    session = self._new_session(conn, user)
                    audit(conn, user["id"], "login", user["id"])
        if throttled:
            raise LoginThrottled("Túl sok bejelentkezési kísérlet. Próbálja később.")
        if failure:
            raise AuthenticationError("Hibás felhasználónév vagy jelszó.")
        self.session = session
        return session

    def logout(self):
        if self.session:
            with transaction(self.db) as conn:
                conn.execute("DELETE FROM auth_sessions WHERE token_hash=?",
                             (hashlib.sha256(self.session.token.encode()).hexdigest(),))
                audit(conn, self.session.user_id, "logout", self.session.user_id)
        self.session = None

    def change_password(self, old_password, new_password):
        encoded = hash_password(new_password)
        with transaction(self.db) as conn:
            user = self._current(conn)
            if not verify_password(old_password, user["password_hash"]):
                raise AuthenticationError("A jelenlegi jelszó hibás.")
            conn.execute("UPDATE users SET password_hash=?,auth_version=auth_version+1 WHERE id=?",
                         (encoded, user["id"]))
            conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user["id"],))
            audit(conn, user["id"], "password_changed", user["id"])
        self.session = None

    def list_users(self):
        with transaction(self.db) as conn:
            self._current(conn, admin=True)
            return [public_user(row) for row in conn.execute("SELECT * FROM users ORDER BY username")]

    def save_user(self, values):
        values = dict(values)
        if set(values) - {"id", "username", "display_name", "role", "active", "password", "auth_version"}:
            raise ValidationError("Ismeretlen felhasználói mező.")
        encoded = hash_password(values["password"]) if "password" in values else None
        with transaction(self.db) as conn:
            actor = self._current(conn, admin=True)
            old = conn.execute("SELECT * FROM users WHERE id=?", (values.get("id"),)).fetchone()
            if values.get("id") and old is None:
                raise ConflictError("A felhasználó nem található.")
            username = self._username(values.get("username", old["username"] if old else ""))
            role = values.get("role", old["role"] if old else "viewer")
            active = values.get("active", old["active"] if old else 1)
            if role not in ROLES or active not in (0, 1, False, True):
                raise ValidationError("Érvénytelen szerepkör vagy állapot.")
            if old and old["active"] and old["role"] == "admin" and (not active or role != "admin"):
                count = conn.execute("SELECT COUNT(*) FROM users WHERE active=1 AND role='admin'").fetchone()[0]
                if count <= 1:
                    raise ValidationError("Az utolsó aktív adminisztrátor nem tiltható le.")
            user_id = old["id"] if old else str(uuid.uuid4())
            name = values.get("display_name", old["display_name"] if old else username)
            if not isinstance(name, str) or len(name) > 256:
                raise ValidationError("Érvénytelen megjelenítési név.")
            try:
                if old:
                    conn.execute("""UPDATE users SET username=?,display_name=?,role=?,active=?,
                        password_hash=?,auth_version=auth_version+1 WHERE id=?""",
                        (username, name, role, int(active), encoded or old["password_hash"], user_id))
                    conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
                else:
                    if encoded is None:
                        raise ValidationError("Új felhasználóhoz jelszó szükséges.")
                    conn.execute("""INSERT INTO users
                        (id,username,display_name,password_hash,role,active,auth_version)
                        VALUES(?,?,?,?,?,?,1)""", (user_id, username, name, encoded, role, int(active)))
            except sqlite3.IntegrityError as exc:
                raise ValidationError("A felhasználónév már létezik.") from exc
            audit(conn, actor["id"], "user_updated" if old else "user_created", user_id,
                  {"role": role, "active": int(active)})
            return public_user(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())

    def change_user_password(self, user_id, password):
        return self.save_user({"id": user_id, "password": password})

    def deactivate_user(self, user_id):
        return self.save_user({"id": user_id, "active": 0})


AuthorizationError = PermissionDenied
