"""Login for hosted use (VOICE_TUTOR_AUTH=on): one email + password account, server-side sessions.

- Passwords: Argon2id (OWASP's first choice), 19 MiB / 2 passes / 1 lane: OWASP's recommended minimum, which
  keeps a login check light enough for a 1 GB server. Hashes are upgraded on login if these settings change.
- Sessions: a random 256-bit token in an HttpOnly cookie; only its SHA-256 is stored, so a copy of the database
  can't be used to sign in. A session ends after 30 days unused or 90 days at most, on logout, on "sign out
  everywhere", and when the password changes.
- Wrong email and wrong password look the same (same message, same work: a dummy hash is checked when the
  email is unknown), so the login form doesn't reveal whether an account exists.
- Repeated failures are throttled per IP and per email (LoginThrottle).
The account is created on the server (`python manage.py user create`); there's no sign-up page.
"""

import hashlib
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

log = logging.getLogger("auth")

SESSION_IDLE_S = 30 * 86400
SESSION_MAX_S = 90 * 86400
TOUCH_EVERY_S = 300  # how often a session's "last used" time is written (not on every request)
MIN_PASSWORD, MAX_PASSWORD = 10, 128
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
_DUMMY_HASH = _hasher.hash("timing-equaliser: never a real password")


class AuthError(ValueError):
    """Something the person can fix (bad email, weak password, account exists); the message says what."""


@dataclass(frozen=True)
class User:
    id: int
    email: str


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not _EMAIL.match(email):
        raise AuthError("Enter a valid email address.")
    return email


def check_password(password: str, email: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"The password needs at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"The password can be at most {MAX_PASSWORD} characters.")
    if email and password.strip().lower() == email.lower():
        raise AuthError("The password can't be your email address.")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthStore:
    """Accounts and sessions in SQLite. Thread-safe (one connection, one lock); every query is indexed and tiny."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    password_changed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    ip TEXT,
                    user_agent TEXT
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
            """)
        try:
            path.chmod(0o600)  # owner only (no effect on Windows, which uses the folder's permissions)
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _one(self, sql: str, args=()) -> tuple | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _run(self, sql: str, args=()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # ---- the account

    def users(self) -> list[User]:
        with self._lock:
            return [User(*r) for r in self._db.execute("SELECT id, email FROM users ORDER BY id")]

    def create_user(self, email: str, password: str) -> User:
        """The one account. Refuses if an account already exists (change its password instead)."""
        email = normalize_email(email)
        check_password(password, email)
        hashed = _hasher.hash(password)
        now = time.time()
        with self._lock:
            if self._db.execute("SELECT 1 FROM users").fetchone():
                raise AuthError("An account already exists. Use `python manage.py user password` to change its "
                                "password, or delete it first.")
            cur = self._db.execute("INSERT INTO users (email, password_hash, created_at, password_changed_at) "
                                   "VALUES (?, ?, ?, ?)", (email, hashed, now, now))
        log.info("Account created: %s", email)
        return User(cur.lastrowid, email)

    def set_password(self, email: str, password: str, keep_session: str | None = None) -> None:
        """New password; every session ends except `keep_session` (the one that changed it, if any)."""
        email = normalize_email(email)
        check_password(password, email)
        hashed = _hasher.hash(password)
        with self._lock:
            row = self._db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if not row:
                raise AuthError(f"No account for {email}.")
            self._db.execute("UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                             (hashed, time.time(), row[0]))
            keep = _token_hash(keep_session) if keep_session else ""
            self._db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (row[0], keep))
        log.info("Password changed for %s; other sessions signed out", email)

    def delete_user(self, email: str) -> bool:
        removed = self._run("DELETE FROM users WHERE email = ?", (normalize_email(email),)) > 0  # sessions cascade
        if removed:
            log.info("Account deleted: %s", email)
        return removed

    def authenticate(self, email: str, password: str) -> User | None:
        """The user if email and password match, else None. Same work either way (no account enumeration)."""
        try:
            email = normalize_email(email)
        except AuthError:
            email = ""
        row = self._one("SELECT id, email, password_hash FROM users WHERE email = ?", (email,)) if email else None
        if len(password or "") > MAX_PASSWORD:  # don't spend hashing work on absurd input
            return None
        try:
            _hasher.verify(row[2] if row else _DUMMY_HASH, password or "")
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return None
        if not row:
            return None
        if _hasher.check_needs_rehash(row[2]):
            self._run("UPDATE users SET password_hash = ? WHERE id = ?", (_hasher.hash(password), row[0]))
        return User(row[0], row[1])

    # ---- sessions

    def create_session(self, user: User, ip: str = "", user_agent: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._run("INSERT INTO sessions (token_hash, user_id, created_at, last_seen, ip, user_agent) "
                  "VALUES (?, ?, ?, ?, ?, ?)", (_token_hash(token), user.id, now, now, ip[:64], user_agent[:300]))
        return token

    def session_user(self, token: str | None) -> User | None:
        """The signed-in user for a session token, or None (unknown, idle too long, or too old)."""
        if not token or len(token) > 100:
            return None
        h = _token_hash(token)
        row = self._one("SELECT s.created_at, s.last_seen, u.id, u.email FROM sessions s "
                        "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (h,))
        if not row:
            return None
        created, last_seen, uid, email = row
        now = time.time()
        if now - last_seen > SESSION_IDLE_S or now - created > SESSION_MAX_S:
            self._run("DELETE FROM sessions WHERE token_hash = ?", (h,))
            return None
        if now - last_seen > TOUCH_EVERY_S:
            self._run("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, h))
        return User(uid, email)

    def end_session(self, token: str | None) -> None:
        if token:
            self._run("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))

    def end_all_sessions(self, user: User | None = None) -> int:
        if user is None:
            return self._run("DELETE FROM sessions")
        return self._run("DELETE FROM sessions WHERE user_id = ?", (user.id,))

    def sessions(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT u.email, s.created_at, s.last_seen, s.ip, s.user_agent FROM sessions s "
                                    "JOIN users u ON u.id = s.user_id ORDER BY s.last_seen DESC").fetchall()
        return [dict(zip(("email", "created_at", "last_seen", "ip", "user_agent"), r)) for r in rows]

    def purge_expired(self) -> int:
        now = time.time()
        return self._run("DELETE FROM sessions WHERE last_seen < ? OR created_at < ?",
                         (now - SESSION_IDLE_S, now - SESSION_MAX_S))


class LoginThrottle:
    """Failed sign-ins per IP and per email in a sliding window; too many locks that key until the window passes.
    In memory: a restart clears it, which only ever loosens the limit for a moment."""

    def __init__(self, window_s: float = 15 * 60, per_email: int = 5, per_ip: int = 20, clock=time.monotonic):
        self.window_s, self.limits, self._clock = window_s, {"email": per_email, "ip": per_ip}, clock
        self._fails: dict[str, deque] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> deque:
        q = self._fails.setdefault(key, deque())
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def retry_after(self, ip: str, email: str) -> int:
        """Seconds until another attempt is allowed (0 = go ahead)."""
        now = self._clock()
        wait = 0.0
        with self._lock:
            for kind, key in (("ip", f"ip:{ip}"), ("email", f"email:{email.strip().lower()}")):
                q = self._recent(key, now)
                if len(q) >= self.limits[kind]:
                    wait = max(wait, q[0] + self.window_s - now)
        return int(wait) + 1 if wait > 0 else 0

    def failed(self, ip: str, email: str) -> None:
        now = self._clock()
        with self._lock:
            for key in (f"ip:{ip}", f"email:{email.strip().lower()}"):
                self._recent(key, now).append(now)
            if len(self._fails) > 10_000:  # bounded memory under a flood of distinct keys
                for k in [k for k, q in self._fails.items() if not self._recent(k, now)]:
                    del self._fails[k]

    def succeeded(self, email: str) -> None:
        with self._lock:
            self._fails.pop(f"email:{email.strip().lower()}", None)
