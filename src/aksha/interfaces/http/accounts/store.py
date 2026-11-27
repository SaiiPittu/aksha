"""The accounts database: SQLite (standard library ``sqlite3``), WAL, foreign keys on.

Accounts are mutable operational data of the HTTP server (who may sign in, with which
role), not facts of any agent's work, so they live in their own file
(``<home>/accounts.sqlite``) and never in the hash-chained event store. Rows here are
updated and deleted as people change their accounts, with one exception: the ``audit``
table is append-only, and triggers refuse any update or delete of it.

The schema is versioned by the ``schema_version`` table; version 1 is created with
idempotent ``CREATE ... IF NOT EXISTS`` statements, and a file written by a newer version
is refused rather than guessed at. One connection serves every thread, under a lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aksha.interfaces.http.accounts.credentials import new_id

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orgs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('main', 'personal')),
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS orgs_one_main ON orgs (kind) WHERE kind = 'main';
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT,
    tenant TEXT NOT NULL UNIQUE,
    is_guest INTEGER NOT NULL DEFAULT 0,
    guest_expires_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    org_id TEXT NOT NULL REFERENCES orgs (id),
    user_id TEXT NOT NULL UNIQUE REFERENCES users (id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
    deactivated_at REAL,
    created_at REAL NOT NULL,
    PRIMARY KEY (org_id, user_id)
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id),
    csrf TEXT NOT NULL,
    remember INTEGER NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions (user_id);
CREATE TABLE IF NOT EXISTS invites (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs (id),
    email TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    token_hash TEXT NOT NULL UNIQUE,
    inviter_id TEXT NOT NULL REFERENCES users (id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    accepted_at REAL,
    accepted_by TEXT REFERENCES users (id)
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users (id),
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    last_used_at REAL,
    revoked_at REAL
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    org_id TEXT,
    actor_id TEXT,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    ip TEXT
);
CREATE INDEX IF NOT EXISTS audit_by_org ON audit (org_id, id);
CREATE INDEX IF NOT EXISTS audit_by_attempt ON audit (action, target, ip, at);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
CREATE TABLE IF NOT EXISTS session_meta (
    tenant TEXT NOT NULL,
    session_id TEXT NOT NULL,
    title TEXT,
    archived_at REAL,
    PRIMARY KEY (tenant, session_id)
);
"""


class StoreRefused(Exception):
    """A row the database refused (a taken email, a second main org) or a newer schema."""

    def __init__(self, code: str, message: str) -> None:
        """Hold the refusal's code and message."""
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Org:
    """An organisation: the main one people sign up or are invited into, or a personal one.

    A guest gets a ``personal`` org of their own at creation, as its Owner and only member,
    and keeps it when the guest is saved as an account: a guest never joins the main org.
    """

    id: str
    name: str
    kind: str
    created_at: float


@dataclass(frozen=True)
class User:
    """A person who can sign in; ``tenant`` is the runtime owner their work is kept under."""

    id: str
    email: str | None
    name: str
    tenant: str
    is_guest: bool
    guest_expires_at: float | None
    created_at: float
    password_hash: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Member:
    """A user in an org, with a role; deactivated members cannot sign in."""

    user: User
    org_id: str
    role: str
    deactivated_at: float | None

    @property
    def active(self) -> bool:
        """Whether the member may sign in."""
        return self.deactivated_at is None


@dataclass(frozen=True)
class AuthSession:
    """A signed-in browser: the digest of its cookie, its CSRF token and when it ends."""

    token_hash: str
    user_id: str
    csrf: str = field(repr=False)
    remember: bool
    created_at: float
    expires_at: float
    last_seen_at: float


@dataclass(frozen=True)
class Invite:
    """An invitation into an org, by link; only the digest of its token is kept."""

    id: str
    org_id: str
    email: str
    role: str
    inviter_id: str
    created_at: float
    expires_at: float
    revoked_at: float | None
    accepted_at: float | None
    accepted_by: str | None

    def state(self, now: float) -> str:
        """``used``, ``revoked``, ``expired`` or ``valid``, in that order of precedence."""
        if self.accepted_at is not None:
            return "used"
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at <= now:
            return "expired"
        return "valid"


@dataclass(frozen=True)
class ApiToken:
    """A named API token of one user; only the digest of its secret is kept."""

    id: str
    user_id: str
    name: str
    created_at: float
    last_used_at: float | None
    revoked_at: float | None


@dataclass(frozen=True)
class AuditRecord:
    """One audit row: when, in which org, who did what to whom, and from where."""

    id: int
    at: float
    org_id: str | None
    actor_id: str | None
    action: str
    target: str | None
    detail: Mapping[str, Any]
    ip: str | None


@dataclass(frozen=True)
class SessionMeta:
    """What the server keeps about a session beside the runtime: a title and an archive flag."""

    tenant: str
    session_id: str
    title: str | None
    archived_at: float | None


_USER = "u.id, u.email, u.name, u.tenant, u.is_guest, u.guest_expires_at, u.created_at, " \
        "u.password_hash"  # fmt: skip


def _user(row: sqlite3.Row) -> User:
    return User(row["id"], row["email"], row["name"], row["tenant"], bool(row["is_guest"]),
                row["guest_expires_at"], row["created_at"], row["password_hash"])  # fmt: skip


def _member(row: sqlite3.Row) -> Member:
    return Member(_user(row), row["org_id"], row["role"], row["deactivated_at"])


def _invite(row: sqlite3.Row) -> Invite:
    return Invite(row["id"], row["org_id"], row["email"], row["role"], row["inviter_id"],
                  row["created_at"], row["expires_at"], row["revoked_at"], row["accepted_at"],
                  row["accepted_by"])  # fmt: skip


def _token(row: sqlite3.Row) -> ApiToken:
    return ApiToken(row["id"], row["user_id"], row["name"], row["created_at"],
                    row["last_used_at"], row["revoked_at"])  # fmt: skip


def _session(row: sqlite3.Row) -> AuthSession:
    return AuthSession(row["token_hash"], row["user_id"], row["csrf"], bool(row["remember"]),
                       row["created_at"], row["expires_at"], row["last_seen_at"])  # fmt: skip


class AccountStore:
    """The accounts database at ``path``; ``clock`` gives the time in epoch seconds."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        """Open (creating if needed) the database and bring its schema to version 1."""
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        try:
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._migrate()
        except BaseException:
            self._db.close()
            raise

    def _migrate(self) -> None:
        with self._db:
            self._db.executescript(_SCHEMA)
            rows = self._db.execute("SELECT version FROM schema_version").fetchall()
            if not rows:
                self._db.execute("INSERT INTO schema_version (version) VALUES (?)",
                                 (SCHEMA_VERSION,))  # fmt: skip
            elif max(r["version"] for r in rows) > SCHEMA_VERSION:
                raise StoreRefused("schema_newer", f"{self.path} was written by a newer Aksha")

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._db:
            yield self._db

    def _one(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._db.execute(sql, args).fetchone()
            return row

    def _all(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, args).fetchall())

    def pragma(self, name: str) -> object:
        """One PRAGMA's value (``journal_mode``, ``foreign_keys``), for checks."""
        row = self._one(f"PRAGMA {name}")
        return None if row is None else row[0]

    # --- orgs and users ---------------------------------------------------------------------

    def create_org(self, name: str, kind: str, *, org_id: str | None = None) -> Org:
        """A new org; there is at most one ``main`` org."""
        org = Org(org_id or new_id("org"), name, kind, self.clock())
        try:
            with self._tx() as db:
                db.execute("INSERT INTO orgs (id, name, kind, created_at) VALUES (?, ?, ?, ?)",
                           (org.id, org.name, org.kind, org.created_at))  # fmt: skip
        except sqlite3.IntegrityError:
            raise StoreRefused("org_exists", "there is already a main org") from None
        return org

    def org(self, org_id: str) -> Org | None:
        """The org with ``org_id``, or None."""
        row = self._one("SELECT * FROM orgs WHERE id = ?", (org_id,))
        return None if row is None else Org(row["id"], row["name"], row["kind"], row["created_at"])

    def org_of_kind(self, kind: str) -> Org | None:
        """The main org (or the oldest of ``kind``), or None before it exists."""
        row = self._one("SELECT * FROM orgs WHERE kind = ? ORDER BY created_at, id LIMIT 1",
                        (kind,))  # fmt: skip
        return None if row is None else Org(row["id"], row["name"], row["kind"], row["created_at"])

    def create_personal(self, user_id: str, name: str, *, tenant: str,
                        guest_expires_at: float | None, org_name: str = "Personal workspace",
                        ) -> Org:  # fmt: skip
        """A user (a guest, when ``guest_expires_at``) and their own org, its Owner, at once."""
        org = Org(new_id("org"), org_name, "personal", self.clock())
        user = User(user_id, None, name, tenant, guest_expires_at is not None, guest_expires_at,
                    self.clock(), None)  # fmt: skip
        with self._tx() as db:
            db.execute("INSERT INTO orgs (id, name, kind, created_at) VALUES (?, ?, ?, ?)",
                       (org.id, org.name, org.kind, org.created_at))  # fmt: skip
            self._insert_user(db, user, org.id, "owner")
        return org

    def create_user(self, user_id: str, email: str | None, name: str, password_hash: str | None,
                    *, tenant: str, guest_expires_at: float | None = None,
                    org_id: str | None = None, role: str | None = None) -> User:  # fmt: skip
        """A new user, in ``org_id`` with ``role`` when given, in one transaction.

        ``email_taken`` when another user has the email, in any case.
        """
        user = User(user_id, email, name, tenant, guest_expires_at is not None, guest_expires_at,
                    self.clock(), password_hash)  # fmt: skip
        with self._tx() as db:
            self._insert_user(db, user, org_id, role)
        return user

    def found_org(self, org_name: str, user: User) -> Org:
        """The main org and its first user, its Owner, in one transaction.

        ``org_exists`` when the server already has its org (sign-up is closed).
        """
        org = Org(new_id("org"), org_name, "main", self.clock())
        with self._tx() as db:
            try:
                db.execute("INSERT INTO orgs (id, name, kind, created_at) VALUES (?, ?, ?, ?)",
                           (org.id, org.name, org.kind, org.created_at))  # fmt: skip
            except sqlite3.IntegrityError:
                raise StoreRefused("org_exists", "the server already has its org") from None
            self._insert_user(db, user, org.id, "owner")
        return org

    def _insert_user(self, db: sqlite3.Connection, user: User, org_id: str | None,
                     role: str | None) -> None:  # fmt: skip
        try:
            db.execute(
                "INSERT INTO users (id, email, name, password_hash, tenant, is_guest, "
                "guest_expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user.id, user.email, user.name, user.password_hash, user.tenant,
                 int(user.is_guest), user.guest_expires_at, user.created_at),
            )  # fmt: skip
        except sqlite3.IntegrityError as e:
            if "email" in str(e):
                raise StoreRefused("email_taken", "an account has this email") from None
            raise StoreRefused("user_exists", f"user {user.id} or its tenant exists") from None
        if org_id is not None:
            db.execute(
                "INSERT INTO memberships (org_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
                (org_id, user.id, role, user.created_at),
            )

    def user(self, user_id: str) -> User | None:
        """The user with ``user_id``, or None."""
        row = self._one(f"SELECT {_USER} FROM users u WHERE u.id = ?", (user_id,))
        return None if row is None else _user(row)

    def user_by_email(self, email: str) -> User | None:
        """The user with ``email``, ignoring case, or None."""
        row = self._one(f"SELECT {_USER} FROM users u WHERE u.email = ?", (email,))
        return None if row is None else _user(row)

    def set_password(self, user_id: str, password_hash: str) -> None:
        """Replace a user's password hash."""
        with self._tx() as db:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))

    def save_guest(self, user_id: str, email: str, name: str, password_hash: str) -> None:
        """Turn a guest into a normal user, keeping the tenant and their personal org."""
        try:
            with self._tx() as db:
                db.execute(
                    "UPDATE users SET email = ?, name = ?, password_hash = ?, is_guest = 0, "
                    "guest_expires_at = NULL WHERE id = ?",
                    (email, name, password_hash, user_id),
                )
        except sqlite3.IntegrityError:
            raise StoreRefused("email_taken", "an account has this email") from None

    # --- memberships ------------------------------------------------------------------------

    def add_membership(self, org_id: str, user_id: str, role: str) -> None:
        """Put a user in an org with a role (a user is in one org)."""
        with self._tx() as db:
            db.execute(
                "INSERT INTO memberships (org_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
                (org_id, user_id, role, self.clock()),
            )

    def member(self, user_id: str) -> Member | None:
        """The user with their org and role, or None."""
        row = self._one(
            f"SELECT {_USER}, m.org_id, m.role, m.deactivated_at FROM users u "
            "JOIN memberships m ON m.user_id = u.id WHERE u.id = ?",
            (user_id,),
        )
        return None if row is None else _member(row)

    def members(self, org_id: str) -> list[Member]:
        """Everyone in ``org_id``, oldest first."""
        rows = self._all(
            f"SELECT {_USER}, m.org_id, m.role, m.deactivated_at FROM users u "
            "JOIN memberships m ON m.user_id = u.id WHERE m.org_id = ? "
            "ORDER BY m.created_at, u.id",
            (org_id,),
        )
        return [_member(r) for r in rows]

    def set_role(self, user_id: str, role: str) -> None:
        """Change a member's role."""
        with self._tx() as db:
            db.execute("UPDATE memberships SET role = ? WHERE user_id = ?", (role, user_id))

    def set_active(self, user_id: str, active: bool) -> None:
        """Deactivate (and drop every session of) a member, or reactivate them."""
        with self._tx() as db:
            db.execute("UPDATE memberships SET deactivated_at = ? WHERE user_id = ?",
                       (None if active else self.clock(), user_id))  # fmt: skip
            if not active:
                db.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))

    # --- sessions ---------------------------------------------------------------------------

    def create_session(self, token_hash: str, user_id: str, *, csrf: str, remember: bool,
                       expires_at: float) -> AuthSession:  # fmt: skip
        """Record a signed-in browser by the digest of its cookie."""
        now = self.clock()
        session = AuthSession(token_hash, user_id, csrf, remember, now, expires_at, now)
        with self._tx() as db:
            db.execute(
                "INSERT INTO auth_sessions (token_hash, user_id, csrf, remember, created_at, "
                "expires_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token_hash, user_id, csrf, int(remember), now, expires_at, now),
            )
        return session

    def session(self, token_hash: str) -> AuthSession | None:
        """The session whose cookie has this digest, or None."""
        row = self._one("SELECT * FROM auth_sessions WHERE token_hash = ?", (token_hash,))
        return None if row is None else _session(row)

    def touch_session(self, token_hash: str, expires_at: float) -> None:
        """Slide a session's expiry: it was used now."""
        with self._tx() as db:
            db.execute(
                "UPDATE auth_sessions SET expires_at = ?, last_seen_at = ? WHERE token_hash = ?",
                (expires_at, self.clock(), token_hash),
            )

    def delete_session(self, token_hash: str) -> None:
        """End one session (sign-out, or an expired one found)."""
        with self._tx() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,))

    def delete_sessions_of(self, user_id: str) -> None:
        """End every session of a user (a password change)."""
        with self._tx() as db:
            db.execute("DELETE FROM auth_sessions WHERE user_id = ?", (user_id,))

    # --- invites ----------------------------------------------------------------------------

    def create_invite(self, invite_id: str, org_id: str, email: str, role: str, token_hash: str,
                      inviter_id: str, expires_at: float) -> Invite:  # fmt: skip
        """Record an invitation by the digest of its link token."""
        now = self.clock()
        with self._tx() as db:
            db.execute(
                "INSERT INTO invites (id, org_id, email, role, token_hash, inviter_id, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (invite_id, org_id, email, role, token_hash, inviter_id, now, expires_at),
            )
        return Invite(invite_id, org_id, email, role, inviter_id, now, expires_at, None, None,
                      None)  # fmt: skip

    def invite(self, invite_id: str) -> Invite | None:
        """The invite with ``invite_id``, or None."""
        row = self._one("SELECT * FROM invites WHERE id = ?", (invite_id,))
        return None if row is None else _invite(row)

    def invite_by_token(self, token_hash: str) -> Invite | None:
        """The invite whose link token has this digest, or None."""
        row = self._one("SELECT * FROM invites WHERE token_hash = ?", (token_hash,))
        return None if row is None else _invite(row)

    def invites(self, org_id: str) -> list[Invite]:
        """Every invite of ``org_id``, newest first."""
        rows = self._all("SELECT * FROM invites WHERE org_id = ? ORDER BY created_at DESC, id",
                         (org_id,))  # fmt: skip
        return [_invite(r) for r in rows]

    def reissue_invite(self, invite_id: str, token_hash: str, expires_at: float) -> None:
        """A new link token and expiry for an invite; the old link stops working."""
        with self._tx() as db:
            db.execute("UPDATE invites SET token_hash = ?, expires_at = ? WHERE id = ?",
                       (token_hash, expires_at, invite_id))  # fmt: skip

    def revoke_invite(self, invite_id: str) -> None:
        """Revoke an invite."""
        with self._tx() as db:
            db.execute("UPDATE invites SET revoked_at = ? WHERE id = ?", (self.clock(), invite_id))

    def accept_invite(self, invite: Invite, user: User) -> None:
        """Create the invited user in the invite's org and mark the invite used, at once."""
        with self._tx() as db:
            self._insert_user(db, user, invite.org_id, invite.role)
            done = db.execute(
                "UPDATE invites SET accepted_at = ?, accepted_by = ? "
                "WHERE id = ? AND accepted_at IS NULL",
                (user.created_at, user.id, invite.id),
            )
            if done.rowcount != 1:
                raise StoreRefused("invite_used", "the invite was used")

    # --- API tokens -------------------------------------------------------------------------

    def create_token(self, token_id: str, user_id: str, name: str, token_hash: str) -> ApiToken:
        """Record an API token by the digest of its secret."""
        now = self.clock()
        with self._tx() as db:
            db.execute(
                "INSERT INTO api_tokens (id, user_id, name, token_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token_id, user_id, name, token_hash, now),
            )
        return ApiToken(token_id, user_id, name, now, None, None)

    def token_by_secret(self, token_hash: str) -> ApiToken | None:
        """The token whose secret has this digest, or None."""
        row = self._one("SELECT * FROM api_tokens WHERE token_hash = ?", (token_hash,))
        return None if row is None else _token(row)

    def tokens(self, user_id: str) -> list[ApiToken]:
        """A user's tokens, newest first."""
        rows = self._all(
            "SELECT * FROM api_tokens WHERE user_id = ? ORDER BY created_at DESC, id", (user_id,)
        )
        return [_token(r) for r in rows]

    def token_used(self, token_id: str) -> None:
        """Note that a token was used now."""
        with self._tx() as db:
            db.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
                       (self.clock(), token_id))  # fmt: skip

    def revoke_token(self, token_id: str) -> None:
        """Revoke a token."""
        with self._tx() as db:
            db.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ?",
                       (self.clock(), token_id))  # fmt: skip

    # --- audit ------------------------------------------------------------------------------

    def audit(self, action: str, *, org_id: str | None, actor_id: str | None,
              target: str | None, detail: Mapping[str, Any], ip: str | None) -> int:  # fmt: skip
        """Append one audit record; returns its ID."""
        with self._tx() as db:
            cursor = db.execute(
                "INSERT INTO audit (at, org_id, actor_id, action, target, detail, ip) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self.clock(), org_id, actor_id, action, target,
                 json.dumps(dict(detail), sort_keys=True), ip),
            )  # fmt: skip
            return int(cursor.lastrowid or 0)

    def audit_records(self, *, org_id: str | None, limit: int = 1000, before: int | None = None,
                      action: str | None = None,
                      actor_id: str | None = None) -> list[AuditRecord]:  # fmt: skip
        """Audit records, newest first; ``org_id=None`` reads every org (for tests and tools)."""
        where: list[str] = ["1 = 1"]
        args: list[object] = []
        for column, value in (("org_id", org_id), ("action", action), ("actor_id", actor_id)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        if before is not None:
            where.append("id < ?")
            args.append(before)
        rows = self._all(
            f"SELECT * FROM audit WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            (*args, limit),
        )
        return [
            AuditRecord(
                r["id"],
                r["at"],
                r["org_id"],
                r["actor_id"],
                r["action"],
                r["target"],
                json.loads(r["detail"]),
                r["ip"],
            )
            for r in rows
        ]

    def actions_from(self, action: str, ip: str | None, since: float) -> int:
        """How many ``action`` audit rows ``ip`` caused since ``since`` (a rate, never stored)."""
        row = self._one("SELECT COUNT(*) FROM audit WHERE action = ? AND ip IS ? AND at > ?",
                        (action, ip, since))  # fmt: skip
        return 0 if row is None else int(row[0])

    def failed_sign_ins(self, email: str, ip: str | None, since: float) -> list[float]:
        """When sign-in failed for ``email`` from ``ip`` since ``since``, newest first.

        Throttling is derived from these audit rows; no counter is kept anywhere.
        """
        rows = self._all(
            "SELECT at FROM audit WHERE action = 'sign_in_failed' AND target = ? "
            "AND ip IS ? AND at > ? ORDER BY at DESC",
            (email.lower(), ip, since),
        )
        return [float(r["at"]) for r in rows]

    # --- session meta -----------------------------------------------------------------------

    def session_meta(self, tenant: str, session_id: str) -> SessionMeta | None:
        """The server's title override and archive flag of one session, or None."""
        row = self._one("SELECT * FROM session_meta WHERE tenant = ? AND session_id = ?",
                        (tenant, session_id))  # fmt: skip
        if row is None:
            return None
        return SessionMeta(row["tenant"], row["session_id"], row["title"], row["archived_at"])

    def sessions_meta(self, tenant: str) -> list[SessionMeta]:
        """Every session meta row of ``tenant``."""
        rows = self._all("SELECT * FROM session_meta WHERE tenant = ? ORDER BY session_id",
                         (tenant,))  # fmt: skip
        return [SessionMeta(r["tenant"], r["session_id"], r["title"], r["archived_at"])
                for r in rows]  # fmt: skip

    def set_session_meta(self, tenant: str, session_id: str, *, title: str | None = None,
                         archived: bool | None = None) -> None:  # fmt: skip
        """Set a session's title (when given) and archive flag (when given)."""
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO session_meta (tenant, session_id) VALUES (?, ?)",
                       (tenant, session_id))  # fmt: skip
            if title is not None:
                db.execute(
                    "UPDATE session_meta SET title = ? WHERE tenant = ? AND session_id = ?",
                    (title, tenant, session_id),
                )
            if archived is not None:
                db.execute(
                    "UPDATE session_meta SET archived_at = ? WHERE tenant = ? AND session_id = ?",
                    (self.clock() if archived else None, tenant, session_id),
                )

    # SessionMetaPort (interfaces/http/ports.py): what the reads router asks of this store.

    def archived(self, owner: str, session: str) -> bool:
        """Whether ``owner`` archived ``session`` (False when never set)."""
        meta = self.session_meta(owner, session)
        return meta is not None and meta.archived_at is not None

    def set_archived(self, owner: str, session: str, archived: bool) -> None:
        """Archive ``session`` for ``owner``, or restore it."""
        self.set_session_meta(owner, session, archived=archived)

    def title(self, owner: str, session: str) -> str | None:
        """The title ``owner`` gave ``session``, or None for the default."""
        meta = self.session_meta(owner, session)
        return None if meta is None else meta.title

    def set_title(self, owner: str, session: str, title: str | None) -> None:
        """Set ``session``'s title for ``owner``; None goes back to the default."""
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO session_meta (tenant, session_id) VALUES (?, ?)",
                       (owner, session))  # fmt: skip
            db.execute("UPDATE session_meta SET title = ? WHERE tenant = ? AND session_id = ?",
                       (title, owner, session))  # fmt: skip
