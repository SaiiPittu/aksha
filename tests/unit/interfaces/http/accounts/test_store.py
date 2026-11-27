"""The accounts database: its schema, its uniqueness rules and an append-only audit table."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.interfaces.http.accounts.credentials import digest
from aksha.interfaces.http.accounts.store import SCHEMA_VERSION, AccountStore, StoreRefused
from aksha.interfaces.http.ports import SessionMetaPort


class Tick:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def store(tmp_path: Path) -> Iterator[AccountStore]:
    s = AccountStore(tmp_path / "accounts.sqlite", clock=Tick())
    yield s
    s.close()


def _raw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def test_the_schema_is_versioned_and_opening_twice_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "accounts.sqlite"
    AccountStore(path).close()
    AccountStore(path).close()
    db = _raw(path)
    try:
        assert db.execute("SELECT version FROM schema_version").fetchall() == [(SCHEMA_VERSION,)]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"orgs", "users", "memberships", "auth_sessions", "invites", "api_tokens",
                "audit", "session_meta", "schema_version"} <= tables  # fmt: skip
    finally:
        db.close()


def test_a_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "accounts.sqlite"
    AccountStore(path).close()
    db = _raw(path)
    with db:
        db.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION + 1,))
    db.close()
    with pytest.raises(StoreRefused, match="newer"):
        AccountStore(path)


def test_wal_and_foreign_keys_are_on(store: AccountStore) -> None:
    assert store.pragma("journal_mode") == "wal"
    assert store.pragma("foreign_keys") == 1
    with pytest.raises(sqlite3.IntegrityError):
        store.add_membership("org-missing", "u-missing", "member")


def test_emails_are_unique_ignoring_case(store: AccountStore) -> None:
    org = store.create_org("Acme", "main")
    store.create_user("u-1", "Ada@Example.org", "Ada", "h", tenant="u-1")
    with pytest.raises(StoreRefused) as raised:
        store.create_user("u-2", "ada@example.ORG", "Ada again", "h", tenant="u-2")
    assert raised.value.code == "email_taken"
    store.add_membership(org.id, "u-1", "owner")
    found = store.user_by_email("ADA@example.org")
    assert found is not None and found.id == "u-1"


def test_one_main_org_and_a_personal_org_per_guest(store: AccountStore) -> None:
    store.create_org("Acme", "main")
    with pytest.raises(StoreRefused):
        store.create_org("Other", "main")
    a = store.create_personal("g-1", "Guest", tenant="g-1", guest_expires_at=1.0)
    b = store.create_personal("g-2", "Guest", tenant="g-2", guest_expires_at=1.0)
    assert a.kind == b.kind == "personal" and a.id != b.id
    for user, org in (("g-1", a), ("g-2", b)):
        found = store.member(user)
        assert found is not None and found.org_id == org.id and found.role == "owner"
        assert [m.user.id for m in store.members(org.id)] == [user]


def test_the_audit_table_refuses_update_and_delete(store: AccountStore, tmp_path: Path) -> None:
    store.audit("sign_in", org_id=None, actor_id="u-1", target="u-1", detail={}, ip="::1")
    db = _raw(tmp_path / "accounts.sqlite")
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE audit SET action = 'x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM audit")
    finally:
        db.close()
    (record,) = store.audit_records(org_id=None)
    assert record.action == "sign_in" and record.ip == "::1"


def test_a_session_is_stored_by_its_digest_never_the_token(
    store: AccountStore, tmp_path: Path
) -> None:
    store.create_user("u-1", "a@example.org", "A", "h", tenant="u-1")
    token = "secret-session-token-value"
    store.create_session(digest(token), "u-1", csrf="c", remember=True, expires_at=2e9)
    assert store.session(digest(token)) is not None
    assert store.session(token) is None
    db = _raw(tmp_path / "accounts.sqlite")
    try:
        rows = db.execute("SELECT * FROM auth_sessions").fetchall()
    finally:
        db.close()
    assert token not in repr(rows) and digest(token) in repr(rows)


def test_session_meta_keeps_a_title_and_an_archive_flag_per_tenant(store: AccountStore) -> None:
    assert store.session_meta("t1", "S-0001") is None
    store.set_session_meta("t1", "S-0001", title="Probes")
    store.set_session_meta("t1", "S-0001", archived=True)
    meta = store.session_meta("t1", "S-0001")
    assert meta is not None and meta.title == "Probes" and meta.archived_at is not None
    assert store.session_meta("t2", "S-0001") is None
    store.set_session_meta("t1", "S-0001", archived=False)
    meta = store.session_meta("t1", "S-0001")
    assert meta is not None and meta.archived_at is None and meta.title == "Probes"


def test_the_store_is_the_reads_routers_session_meta_port(store: AccountStore) -> None:
    port: SessionMetaPort = store
    assert port.archived("t1", "S-1") is False and port.title("t1", "S-1") is None
    port.set_title("t1", "S-1", "Probes")
    port.set_archived("t1", "S-1", True)
    assert port.archived("t1", "S-1") and port.title("t1", "S-1") == "Probes"
    assert port.archived("t2", "S-1") is False
    port.set_title("t1", "S-1", None)
    port.set_archived("t1", "S-1", False)
    assert port.title("t1", "S-1") is None and port.archived("t1", "S-1") is False
