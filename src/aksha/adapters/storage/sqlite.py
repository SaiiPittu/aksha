"""``SQLiteEventStore``: the event store in one SQLite file (ADR-P01 D1).

The zero-setup backend for local use, tests and the offline reviewer run; PostgreSQL is
the production backend, and both pass one contract suite. WAL mode with
``synchronous=FULL``; triggers refuse ``UPDATE`` and ``DELETE`` on every table; one
writer under ``fcntl.flock`` on ``var/aksha.lock`` (the kernel frees it however the
process dies); readers open the file read-only. A one-row ``meta`` table records the
schema version, checked on every open. Standard library only.

Hash chain (ADR-P01 Amendment H): the one-row ``store_meta`` holds the store id and the
genesis hash; each append runs under ``BEGIN IMMEDIATE``, gives each event the next seq
itself and stores its ``prev_hash`` and ``hash`` (:mod:`aksha.runtime.storage.chain`).
``verify()`` also requires each stored payload and artifact content to be exactly the
canonical JSON the writer stores, so the text cannot differ from the value the hash covers.

Subscriptions are woken at once by this handle's own commits and otherwise poll, which
is how a read-only handle (a web server) follows the writer (the CLI).
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from aksha.adapters.storage.decoded import RowMemo
from aksha.runtime.canonical import canonical_json, freeze, iso_utc
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.execution.ports import Clock
from aksha.runtime.storage.append import (
    SYSTEM_OWNER,
    PreparedBatch,
    check_owner,
    prepare_append,
)
from aksha.runtime.storage.chain import (
    Anchor,
    ChainBreak,
    ChainLink,
    StoreIdentity,
    UnreadableArtifact,
    UnreadableLink,
    new_store_id,
    next_link,
    verify_chain,
)
from aksha.runtime.storage.errors import (
    AlreadyRecorded,
    LockHeld,
    NotWriter,
    SequenceConflict,
    StoreError,
)
from aksha.runtime.storage.rows import ArtifactRow, StoredArtifact
from aksha.runtime.storage.schema import SCHEMA_VERSION, check_schema_version

POLL_S = 0.05
"""How often a subscription re-reads when no commit by this handle woke it."""

_EVENT_COLUMNS = (
    "seq, owner, type, payload, run, instance, step, session, uniq, at, boot, schema_version"
)
_CHAIN_COLUMNS = f"{_EVENT_COLUMNS}, prev_hash, hash"
_ARTIFACT_COLUMNS = (
    "owner, ref, kind, key, version, content, content_hash, canonical_hash, event_seq"
)

SCHEMA = """
CREATE TABLE meta (
  one INTEGER PRIMARY KEY CHECK (one = 1),       -- one row only
  schema_version INTEGER NOT NULL
);
CREATE TABLE store_meta (
  one INTEGER PRIMARY KEY CHECK (one = 1),       -- one row only
  store_id TEXT NOT NULL, created_at TEXT NOT NULL,
  chain_version INTEGER NOT NULL, genesis_hash TEXT NOT NULL
);
CREATE TABLE boots (
  boot INTEGER PRIMARY KEY AUTOINCREMENT,
  pid  INTEGER NOT NULL,
  at   TEXT NOT NULL
);
CREATE TABLE events (
  seq INTEGER PRIMARY KEY,                       -- assigned by the writer: last + 1
  owner TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL,
  run TEXT, instance TEXT, step TEXT, session TEXT,
  uniq TEXT,                                     -- the dedup key
  at TEXT NOT NULL, boot INTEGER NOT NULL, schema_version INTEGER NOT NULL,
  prev_hash TEXT NOT NULL UNIQUE,                -- unique: the chain cannot fork
  hash TEXT NOT NULL UNIQUE,
  UNIQUE (owner, uniq)
);
CREATE INDEX ev_owner_seq  ON events(owner, seq);
CREATE INDEX ev_owner_run  ON events(owner, run, seq);
CREATE INDEX ev_owner_type ON events(owner, type, seq);
CREATE TABLE artifacts (
  owner TEXT NOT NULL, ref TEXT NOT NULL, kind TEXT NOT NULL, key TEXT,
  version INTEGER NOT NULL, content TEXT NOT NULL,
  content_hash TEXT NOT NULL, canonical_hash TEXT NOT NULL,
  event_seq INTEGER NOT NULL REFERENCES events(seq),
  PRIMARY KEY (owner, ref)
);
"""

_TRIGGERS = "".join(
    f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
    "BEGIN SELECT RAISE(ABORT, 'append-only'); END;\n"
    for table in ("events", "artifacts", "boots", "meta", "store_meta")
    for verb in ("UPDATE", "DELETE")
)


def lock_path(path: Path) -> Path:
    """The writer's lock file beside the store: ``var/aksha.sqlite`` locks ``var/aksha.lock``."""
    return path.parent / f"{path.stem}.lock"


def _take_lock(path: Path) -> int:
    """Take the writer lock and write our pid into it; raise ``LockHeld`` naming the holder."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = os.pread(fd, 32, 0).decode(errors="replace").strip()
        os.close(fd)
        raise LockHeld(int(holder) if holder.isdigit() else None) from None
    os.ftruncate(fd, 0)
    os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
    return fd


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
    return row.fetchone() is not None


def _schema_version(conn: sqlite3.Connection) -> int | None:
    """The recorded schema version, or None when the store has no ``meta`` row."""
    if not _has_table(conn, "meta"):
        return None
    row = conn.execute("SELECT schema_version FROM meta").fetchone()
    return None if row is None else int(row[0])


def _identity(conn: sqlite3.Connection) -> StoreIdentity:
    """The ``store_meta`` row; a store without one is refused as another schema."""
    row = conn.execute(
        "SELECT store_id, created_at, chain_version, genesis_hash FROM store_meta"
    ).fetchone()
    if row is None:
        raise StoreError("schema_mismatch", "the event store has no store_meta row")
    return StoreIdentity.read(str(row[0]), str(row[1]), int(row[2]), str(row[3]))


def _create_or_check(
    conn: sqlite3.Connection, created_at: str, store_id: str | None
) -> StoreIdentity:
    """Create the schema, its version and the store identity in one transaction.

    For an existing store, check its schema version instead and, when ``store_id`` is
    given, that it is this store.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _has_table(conn, "events") or _has_table(conn, "meta"):
            check_schema_version(_schema_version(conn))
            identity = _identity(conn)
            identity.check_id(store_id)
        else:
            for statement in (SCHEMA + _TRIGGERS).split(";\n"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO meta (one, schema_version) VALUES (1, ?)", (SCHEMA_VERSION,))
            identity = StoreIdentity.create(store_id or new_store_id(), created_at)
            conn.execute(
                "INSERT INTO store_meta VALUES (1, ?, ?, ?, ?)",
                (identity.store_id, identity.created_at, identity.chain_version,
                 identity.genesis_hash),
            )  # fmt: skip
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return identity


def _stored_head(conn: sqlite3.Connection, identity: StoreIdentity) -> tuple[int, str]:
    """The last row's seq and hash, or ``(0, genesis_hash)`` for an empty store."""
    row = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return (0, identity.genesis_hash) if row is None else (int(row[0]), str(row[1]))


def _event(row: tuple[Any, ...]) -> Event:
    seq, owner, type_, payload, run, instance, step, session, uniq, at, boot, version = row
    return Event(
        type=type_,
        payload=freeze(json.loads(payload)),
        run=run,
        instance=instance,
        step=step,
        session=session,
        dedup_key=uniq,
        seq=seq,
        owner=owner,
        at=at,
        boot=boot,
        schema_version=version,
    )


def _canonical_or_why(text: str) -> tuple[Any, str | None]:
    """Decode stored JSON text; the value, or None and why it is not the writer's text.

    The writer always stores :func:`canonical_json` of the value, and the chain covers the
    decoded value, so text that does not decode, or decodes but is not canonical (a
    duplicated key, other spacing), is reported rather than trusted: SQLite's own JSON
    functions could read a different value from it than the one the hash covers.
    """
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return None, "payload is not JSON"
    try:
        canonical = canonical_json(value) == text
    except (TypeError, ValueError):
        canonical = False
    return (value, None) if canonical else (None, "payload is not in canonical JSON")


def _link(row: tuple[Any, ...]) -> ChainLink | UnreadableLink:
    """One ``_CHAIN_COLUMNS`` row as a chain link, or unreadable if its payload is bad."""
    _, why = _canonical_or_why(str(row[3]))
    if why is not None:
        return UnreadableLink(int(row[0]), str(row[12]), str(row[13]), why)
    return ChainLink(_event(row[:12]), str(row[12]), str(row[13]))


def _checked_artifact(row: tuple[Any, ...]) -> StoredArtifact | UnreadableArtifact:
    """One artifact row for the walk, or unreadable if its content is bad."""
    _, why = _canonical_or_why(str(row[5]))
    if why is not None:
        return UnreadableArtifact(
            str(row[0]), str(row[1]), int(row[8]), why.replace("payload", "content")
        )
    return _artifact(row)


def _artifact(row: tuple[Any, ...]) -> StoredArtifact:
    owner, ref, kind, key, version, content, content_hash, canonical_hash, event_seq = row
    artifact = ArtifactRow(
        ref, kind, key, version, json.loads(content), content_hash, canonical_hash
    )
    return StoredArtifact(owner=owner, event_seq=event_seq, row=artifact)


class SQLiteSubscription:
    """A :class:`~aksha.runtime.storage.ports.Subscription` over a SQLite store."""

    def __init__(self, store: SQLiteEventStore, owner: str, after_seq: int) -> None:
        """Follow ``owner`` in ``store`` from ``after_seq``."""
        self._store = store
        self._owner = owner
        self._cursor = after_seq
        self._closed = False

    @property
    def cursor(self) -> int:
        """The seq of the last event returned."""
        return self._cursor

    def wait(self, timeout: float) -> tuple[Event, ...]:
        """Events after the cursor, waiting up to ``timeout`` seconds for a commit."""
        deadline = time.monotonic() + timeout
        while True:
            events = tuple(self._store.read(self._owner, after_seq=self._cursor))
            if events:
                self._cursor = events[-1].seq
                return events
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._closed:
                return ()
            with self._store.committed:
                self._store.committed.wait(min(POLL_S, remaining))

    def close(self) -> None:
        """Stop following; a later ``wait`` returns at once."""
        self._closed = True


class SQLiteEventStore:
    """The :class:`~aksha.runtime.storage.ports.EventStore` over one SQLite file."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        clock: Clock,
        catalogue: Catalogue,
        *,
        boot: int,
        lock_fd: int | None,
        identity: StoreIdentity,
    ) -> None:
        """Wrap an open connection; use :func:`open_store` rather than calling this."""
        self._conn: sqlite3.Connection | None = conn
        self._clock = clock
        self._catalogue = catalogue
        self._boot = boot
        self._lock_fd = lock_fd
        self._identity = identity
        self._head = _stored_head(conn, identity)
        """The writer's last committed ``(seq, hash)``; moves only after a commit."""
        self._mutex = threading.RLock()
        self._events = RowMemo(_event)
        """Decoded events by seq, reused only while the stored row is unchanged."""
        self._artifacts = RowMemo(_artifact)
        """Decoded artifact rows by (owner, ref), reused only while the row is unchanged."""
        self.committed = threading.Condition()
        """Notified after every commit by this handle, to wake its subscriptions."""

    @property
    def catalogue(self) -> Catalogue:
        """The event types this store accepts."""
        return self._catalogue

    @property
    def is_writer(self) -> bool:
        """Whether this handle holds the writer lock."""
        return self._lock_fd is not None

    def _event(self, row: tuple[Any, ...]) -> Event:
        return self._events.get(row[0], row)

    def _artifact(self, row: tuple[Any, ...]) -> StoredArtifact:
        return self._artifacts.get((row[0], row[1]), row)

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreError("store_closed", "this event store handle is closed")
        return self._conn

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._mutex:
            return self._db().execute(sql, params).fetchall()

    def pragma(self, name: str) -> str:
        """The value of ``PRAGMA name``, as text (SQLite only; for checks and tests)."""
        rows = self._query(f"PRAGMA {name}")
        return str(rows[0][0])

    def append(
        self,
        owner: str,
        boundary: str,
        events: Sequence[NewEvent],
        artifacts: Sequence[ArtifactRow] = (),
        *,
        expected_seq: int | None = None,
    ) -> tuple[Event, ...]:
        """Commit one boundary in one ``BEGIN IMMEDIATE`` transaction; see the port."""
        if not self.is_writer:
            self._db()
            raise NotWriter()
        batch = prepare_append(self._catalogue, owner, boundary, events, artifacts)
        with self._mutex:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                at = iso_utc(self._clock.now())  # inside the lock, so ``at`` follows seq
                if expected_seq is not None and (actual := self._last(owner)) != expected_seq:
                    raise SequenceConflict(owner, expected_seq, actual)
                self._refuse_recorded(batch)
                self._refuse_moved_head(db)
                seq, prev = self._head
                stored: list[Event] = []
                for e in batch.events:
                    seq += 1
                    link = next_link(e.stored(seq=seq, owner=owner, at=at, boot=self._boot), prev)
                    db.execute(
                        "INSERT INTO events (seq, owner, type, payload, run, instance, step,"
                        " session, uniq, at, boot, schema_version, prev_hash, hash)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (seq, owner, e.type, e.payload_json, e.run, e.instance, e.step,
                         e.session, e.dedup_key, at, self._boot, e.schema_version,
                         link.prev_hash, link.hash),
                    )  # fmt: skip
                    stored.append(link.event)
                    prev = link.hash
                for index, row in batch.artifacts:
                    db.execute(
                        "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?)",
                        (owner, row.ref, row.kind, row.key, row.version,
                         canonical_json(row.content), row.content_hash, row.canonical_hash,
                         stored[index].seq),
                    )  # fmt: skip
                db.execute("COMMIT")
                self._head = (seq, prev)
            except sqlite3.IntegrityError as e:
                db.execute("ROLLBACK")
                self._refuse_recorded(batch)
                raise StoreError("constraint_violated", str(e)) from None
            except BaseException:
                db.execute("ROLLBACK")
                raise
        with self.committed:
            self.committed.notify_all()
        return tuple(stored)

    def _refuse_moved_head(self, db: sqlite3.Connection) -> None:
        """Refuse to chain onto a head this writer did not write (rows removed or added)."""
        stored = _stored_head(db, self._identity)
        if stored != self._head:
            raise StoreError(
                "chain_head_moved",
                f"the stored head is seq {stored[0]}, this writer last wrote seq {self._head[0]}",
            )

    def _refuse_recorded(self, batch: PreparedBatch) -> None:
        """Raise ``AlreadyRecorded`` if any of the batch's dedup keys is already stored."""
        keys = batch.dedup_keys()
        if not keys:
            return
        marks = ",".join("?" * len(keys))
        rows = self._query(
            f"SELECT {_EVENT_COLUMNS} FROM events WHERE owner = ? AND uniq IN ({marks})",
            (batch.owner, *keys),
        )
        existing = batch.first_conflict({r[8]: self._event(r) for r in rows})
        if existing is not None:
            raise AlreadyRecorded(existing)

    def read(
        self,
        owner: str,
        *,
        after_seq: int = 0,
        upto_seq: int | None = None,
        run: str | None = None,
        types: frozenset[str] | None = None,
    ) -> Iterator[Event]:
        """The owner's events after the cursor ``after_seq``, in seq order."""
        sql = f"SELECT {_EVENT_COLUMNS} FROM events WHERE owner = ? AND seq > ?"
        params: list[Any] = [check_owner(owner), after_seq]
        if upto_seq is not None:
            sql += " AND seq <= ?"
            params.append(upto_seq)
        if run is not None:
            sql += " AND run = ?"
            params.append(run)
        if types is not None:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            params.extend(sorted(types))
        return iter([self._event(r) for r in self._query(sql + " ORDER BY seq", params)])

    def read_artifacts(
        self, owner: str, *, upto_seq: int | None = None
    ) -> tuple[StoredArtifact, ...]:
        """The owner's artifact rows saved at or before ``upto_seq``."""
        sql = f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts WHERE owner = ?"
        params: list[Any] = [check_owner(owner)]
        if upto_seq is not None:
            sql += " AND event_seq <= ?"
            params.append(upto_seq)
        rows = self._query(sql + " ORDER BY event_seq, ref", params)
        return tuple(self._artifact(r) for r in rows)

    def _last(self, owner: str) -> int:
        rows = self._query("SELECT MAX(seq) FROM events WHERE owner = ?", (owner,))
        return int(rows[0][0] or 0)

    def last_seq(self, owner: str) -> int:
        """The owner's latest seq, 0 when it has none."""
        return self._last(check_owner(owner))

    def by_dedup_key(self, owner: str, key: str) -> Event | None:
        """The owner's event holding dedup key ``key``."""
        rows = self._query(
            f"SELECT {_EVENT_COLUMNS} FROM events WHERE owner = ? AND uniq = ?",
            (check_owner(owner), key),
        )
        return self._event(rows[0]) if rows else None

    def owners_with_open_runs(self) -> tuple[str, ...]:
        """Owners with a started run that has not ended, sorted."""
        rows = self._query(
            "SELECT DISTINCT s.owner FROM events s WHERE s.type = 'run.started' AND s.owner != ?"
            " AND NOT EXISTS (SELECT 1 FROM events e WHERE e.owner = s.owner"
            " AND e.type = 'run.ended' AND e.run = s.run) ORDER BY s.owner",
            (SYSTEM_OWNER,),
        )
        return tuple(r[0] for r in rows)

    def owners_with_pending_timers(self) -> tuple[str, ...]:
        """Owners with a scheduled timer no ``tclose:`` key closed yet, sorted (ADR-P25 D1)."""
        rows = self._query(
            "SELECT DISTINCT s.owner FROM events s WHERE s.type = 'timer.scheduled'"
            " AND s.owner != ? AND NOT EXISTS (SELECT 1 FROM events c"
            " WHERE c.owner = s.owner AND c.uniq = 'tclose:' || substr(s.uniq, 7))"
            " ORDER BY s.owner",
            (SYSTEM_OWNER,),
        )
        return tuple(r[0] for r in rows)

    def subscribe(self, owner: str, *, after_seq: int = 0) -> SQLiteSubscription:
        """Follow the owner's events after ``after_seq``."""
        check_owner(owner)
        self._db()
        return SQLiteSubscription(self, owner, after_seq)

    def identity(self) -> StoreIdentity:
        """The store's ``store_meta`` row."""
        self._db()
        return self._identity

    def head(self) -> tuple[int, str]:
        """The last seq and its hash, across all owners."""
        with self._mutex:
            return _stored_head(self._db(), self._identity)

    def verify(self, *, anchors: Sequence[Anchor] = ()) -> ChainBreak | None:
        """Walk the whole chain in seq order; see the port."""
        rows = self._query(f"SELECT {_CHAIN_COLUMNS} FROM events ORDER BY seq")
        saved = self._query(f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts ORDER BY event_seq, ref")
        return verify_chain(
            self._identity,
            (_link(r) for r in rows),
            anchors,
            artifacts=[_checked_artifact(r) for r in saved],
        )

    def close(self) -> None:
        """Close the connection and release the writer lock. Idempotent."""
        with self._mutex:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            if self._lock_fd is not None:
                os.close(self._lock_fd)  # closing the descriptor releases the flock
                self._lock_fd = None


def open_store(
    path: Path,
    clock: Clock,
    *,
    writer: bool,
    catalogue: Catalogue | None = None,
    store_id: str | None = None,
) -> SQLiteEventStore:
    """Open the SQLite event store at ``path``.

    ``writer=True`` takes ``fcntl.flock(LOCK_EX | LOCK_NB)`` on :func:`lock_path` first and
    raises ``LockHeld`` (exit 8) if another handle holds it, then creates the schema (or
    checks the recorded schema version of an existing file) and records this process start
    in ``boots``. ``writer=False`` opens the existing file read-only, checks its schema
    version and never takes the lock. Either raises ``StoreError("schema_mismatch")`` for a
    file of another shape. ``catalogue`` defaults to the core types.

    A writer that creates the store records its identity (``store_meta``): ``store_id``, or
    a random one, and the clock's time. Given for an existing store, ``store_id`` must be
    that store's, or the open is refused with ``store_id_mismatch``.
    """
    catalogue = catalogue or Catalogue(CORE_EVENT_TYPES)
    if not writer:
        if not path.exists():
            raise StoreError("store_missing", f"no event store at {path}")
        conn = sqlite3.connect(
            f"file:{path}?mode=ro", uri=True, isolation_level=None, check_same_thread=False
        )
        conn.execute("PRAGMA query_only = 1")
        try:
            check_schema_version(_schema_version(conn))
            identity = _identity(conn)
            identity.check_id(store_id)
        except BaseException:
            conn.close()
            raise
        return SQLiteEventStore(conn, clock, catalogue, boot=0, lock_fd=None, identity=identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = _take_lock(lock_path(path))
    try:
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    except BaseException:
        os.close(fd)
        raise
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        identity = _create_or_check(conn, iso_utc(clock.now()), store_id)
        cur = conn.execute(
            "INSERT INTO boots (pid, at) VALUES (?, ?)", (os.getpid(), iso_utc(clock.now()))
        )
        boot = int(cur.lastrowid or 0)
    except BaseException:
        conn.close()
        os.close(fd)
        raise
    return SQLiteEventStore(conn, clock, catalogue, boot=boot, lock_fd=fd, identity=identity)
