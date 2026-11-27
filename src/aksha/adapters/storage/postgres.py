"""``PostgresEventStore``: the event store in PostgreSQL (extra ``aksha-harness[postgres]``).

The production backend. It keeps the SQLite adapter's rules, enforced by PostgreSQL
itself, and passes the same contract suite:

1. Append-only: a trigger refuses ``UPDATE``, ``DELETE`` and ``TRUNCATE`` on every table.
2. One writer: the writer's session holds a session advisory lock
   (``pg_try_advisory_lock``), which the server frees when the session ends, however the
   process died. The holder names itself in ``application_name``, so ``LockHeld`` can
   name its pid. Readers set ``default_transaction_read_only``.
3. ``seq`` is assigned by the writer as the last seq plus one, inside the append
   transaction, so a rolled-back boundary leaves no gap (an identity column would, and a
   gap reads as a deleted row in the hash chain).
4. Dedup keys sit under a unique index on ``(owner, uniq)``; a boundary is one transaction.
5. Subscribers ``LISTEN`` on ``aksha_events``; each append sends ``pg_notify`` inside its
   transaction, so the wake-up is delivered exactly when the commit is visible.
6. A one-row ``meta`` table records the schema version, written with the tables and
   checked on every open (``schema_mismatch`` otherwise).
7. Hash chain (ADR-P01 Amendment H): the one-row ``store_meta`` holds the store id and
   the genesis hash. Each append also takes ``pg_advisory_xact_lock`` on the writer key,
   so a second writer that slipped past the session lock still serialises, and stores
   each event's ``prev_hash`` and ``hash`` (:mod:`aksha.runtime.storage.chain`). Payloads
   are ``JSONB``, so the hash is computed over the decoded value in canonical form, never
   over the text PostgreSQL returns; that is why both backends produce the same hashes.

The driver (``psycopg`` 3) is imported only inside this module, and only when a store is
opened, so the runtime and the SQLite path never need it. Needs PostgreSQL 14 or newer.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator, Sequence
from types import ModuleType
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    import psycopg

WRITER_LOCK_KEY = 0x616B7368
"""The advisory lock key (``"aksh"``), below 2**31 so ``pg_locks.objid`` shows it as is."""

CHANNEL = "aksha_events"
"""The ``LISTEN``/``NOTIFY`` channel; the payload is the owner whose events committed."""

_WRITER_APP = "aksha-writer:"

_EVENT_COLUMNS = (
    "seq, owner, type, payload, run, instance, step, session, uniq, at, boot, schema_version"
)
_CHAIN_COLUMNS = f"{_EVENT_COLUMNS}, prev_hash, hash"
_ARTIFACT_COLUMNS = (
    "owner, ref, kind, key, version, content, content_hash, canonical_hash, event_seq"
)

SCHEMA = """
CREATE TABLE meta (
  one INTEGER PRIMARY KEY CHECK (one = 1),
  schema_version INTEGER NOT NULL
);
CREATE TABLE store_meta (
  one INTEGER PRIMARY KEY CHECK (one = 1),
  store_id TEXT NOT NULL, created_at TEXT NOT NULL,
  chain_version INTEGER NOT NULL, genesis_hash TEXT NOT NULL
);
CREATE TABLE boots (
  boot BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pid  INTEGER NOT NULL,
  at   TEXT NOT NULL
);
CREATE TABLE events (
  seq BIGINT PRIMARY KEY,
  owner TEXT NOT NULL, type TEXT NOT NULL, payload JSONB NOT NULL,
  run TEXT, instance TEXT, step TEXT, session TEXT,
  uniq TEXT,
  at TEXT NOT NULL, boot BIGINT NOT NULL, schema_version INTEGER NOT NULL,
  prev_hash TEXT NOT NULL UNIQUE,
  hash TEXT NOT NULL UNIQUE
);
CREATE UNIQUE INDEX ev_owner_uniq ON events (owner, uniq) WHERE uniq IS NOT NULL;
CREATE INDEX ev_owner_seq  ON events (owner, seq);
CREATE INDEX ev_owner_run  ON events (owner, run, seq);
CREATE INDEX ev_owner_type ON events (owner, type, seq);
CREATE TABLE artifacts (
  owner TEXT NOT NULL, ref TEXT NOT NULL, kind TEXT NOT NULL, key TEXT,
  version INTEGER NOT NULL, content JSONB NOT NULL,
  content_hash TEXT NOT NULL, canonical_hash TEXT NOT NULL,
  event_seq BIGINT NOT NULL REFERENCES events (seq),
  PRIMARY KEY (owner, ref)
);
CREATE FUNCTION aksha_refuse_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only: % on % is refused', TG_OP, TG_TABLE_NAME;
END $$;
"""

_TRIGGERS = "".join(
    f"CREATE TRIGGER {table}_no_change BEFORE UPDATE OR DELETE ON {table} "
    "FOR EACH ROW EXECUTE FUNCTION aksha_refuse_change();\n"
    f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
    "FOR EACH STATEMENT EXECUTE FUNCTION aksha_refuse_change();\n"
    for table in ("events", "artifacts", "boots", "meta", "store_meta")
)


def driver() -> ModuleType:
    """Import ``psycopg``, or refuse with ``driver_missing`` naming the extra to install."""
    try:
        import psycopg
    except ImportError:
        raise StoreError(
            "driver_missing",
            "the PostgreSQL adapter needs psycopg: pip install 'aksha-harness[postgres]'",
        ) from None
    module: ModuleType = psycopg
    return module


def _connect(dsn: str, **kwargs: Any) -> psycopg.Connection[tuple[Any, ...]]:
    pg = driver()
    try:
        conn: psycopg.Connection[tuple[Any, ...]] = pg.connect(dsn, autocommit=True, **kwargs)
    except pg.OperationalError as e:
        raise StoreError("store_unreachable", f"cannot reach PostgreSQL: {e}") from None
    return conn


def _event(row: tuple[Any, ...]) -> Event:
    seq, owner, type_, payload, run, instance, step, session, uniq, at, boot, version = row
    return Event(
        type=type_,
        payload=freeze(payload),
        run=run,
        instance=instance,
        step=step,
        session=session,
        dedup_key=uniq,
        seq=int(seq),
        owner=owner,
        at=at,
        boot=int(boot),
        schema_version=version,
    )


def _artifact(row: tuple[Any, ...]) -> StoredArtifact:
    owner, ref, kind, key, version, content, content_hash, canonical_hash, event_seq = row
    artifact = ArtifactRow(ref, kind, key, version, content, content_hash, canonical_hash)
    return StoredArtifact(owner=owner, event_seq=int(event_seq), row=artifact)


def _require_utf8(conn: psycopg.Connection[tuple[Any, ...]]) -> None:
    """Refuse a database whose server encoding is not UTF8 (``encoding_unsupported``).

    Under ``SQL_ASCII`` or ``LATIN1`` a payload SQLite accepts could fail here, or come
    back as other characters, and both backends must accept the same facts (B3).
    """
    row = conn.execute("SHOW server_encoding").fetchone()
    encoding = str(row[0]) if row else ""
    if encoding.upper() != "UTF8":
        raise StoreError(
            "encoding_unsupported", f"the database's server_encoding is {encoding}, not UTF8"
        )


def _has_table(conn: psycopg.Connection[tuple[Any, ...]], name: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()
    return bool(row and row[0])


def _schema_version(conn: psycopg.Connection[tuple[Any, ...]]) -> int | None:
    """The recorded schema version, or None when the database has no ``meta`` row."""
    if not _has_table(conn, "meta"):
        return None
    row = conn.execute("SELECT schema_version FROM meta").fetchone()
    return None if row is None else int(row[0])


def _identity(conn: psycopg.Connection[tuple[Any, ...]]) -> StoreIdentity:
    """The ``store_meta`` row; a database without one is refused as another schema."""
    row = conn.execute(
        "SELECT store_id, created_at, chain_version, genesis_hash FROM store_meta"
    ).fetchone()
    if row is None:
        raise StoreError("schema_mismatch", "the event store has no store_meta row")
    return StoreIdentity.read(str(row[0]), str(row[1]), int(row[2]), str(row[3]))


def _create_or_check(
    conn: psycopg.Connection[tuple[Any, ...]], created_at: str, store_id: str | None
) -> StoreIdentity:
    """Create the schema, its version and the store identity; inside a transaction.

    For an existing store, check its schema version instead and, when ``store_id`` is
    given, that it is this store.
    """
    if _has_table(conn, "events") or _has_table(conn, "meta"):
        check_schema_version(_schema_version(conn))
        identity = _identity(conn)
        identity.check_id(store_id)
        return identity
    conn.execute(SCHEMA + _TRIGGERS)
    conn.execute("INSERT INTO meta (one, schema_version) VALUES (1, %s)", (SCHEMA_VERSION,))
    identity = StoreIdentity.create(store_id or new_store_id(), created_at)
    conn.execute(
        "INSERT INTO store_meta VALUES (1, %s, %s, %s, %s)",
        (identity.store_id, identity.created_at, identity.chain_version, identity.genesis_hash),
    )
    return identity


def _stored_head(
    conn: psycopg.Connection[tuple[Any, ...]], identity: StoreIdentity
) -> tuple[int, str]:
    """The last row's seq and hash, or ``(0, genesis_hash)`` for an empty store."""
    row = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    return (0, identity.genesis_hash) if row is None else (int(row[0]), str(row[1]))


def _lock_holder(conn: psycopg.Connection[tuple[Any, ...]]) -> int | None:
    """The pid the writer session put in its ``application_name``, if visible."""
    row = conn.execute(
        "SELECT a.application_name FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
        " WHERE l.locktype = 'advisory' AND l.granted AND l.classid = 0 AND l.objid = %s"
        " AND l.objsubid = 1 AND l.database = (SELECT oid FROM pg_database"
        " WHERE datname = current_database())",
        (WRITER_LOCK_KEY,),
    ).fetchone()
    name = str(row[0]) if row and row[0] else ""
    pid = name.removeprefix(_WRITER_APP)
    return int(pid) if name.startswith(_WRITER_APP) and pid.isdigit() else None


class PostgresSubscription:
    """A :class:`~aksha.runtime.storage.ports.Subscription` woken by ``LISTEN``/``NOTIFY``."""

    def __init__(self, store: PostgresEventStore, owner: str, after_seq: int, dsn: str) -> None:
        """Open a listening connection, then follow ``owner`` from ``after_seq``.

        ``LISTEN`` runs before the first read, so no commit can fall between them.
        """
        self._store = store
        self._owner = owner
        self._cursor = after_seq
        self._listen: psycopg.Connection[tuple[Any, ...]] | None = _connect(dsn)
        self._listen.execute(f"LISTEN {CHANNEL}")

    @property
    def cursor(self) -> int:
        """The seq of the last event returned."""
        return self._cursor

    def wait(self, timeout: float) -> tuple[Event, ...]:
        """Events after the cursor, waiting up to ``timeout`` seconds for a notification."""
        deadline = time.monotonic() + timeout
        while True:
            events = tuple(self._store.read(self._owner, after_seq=self._cursor))
            if events:
                self._cursor = events[-1].seq
                return events
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._listen is None:
                return ()
            for _ in self._listen.notifies(timeout=remaining, stop_after=1):
                pass

    def close(self) -> None:
        """Close the listening connection. Idempotent."""
        if self._listen is not None:
            self._listen.close()
            self._listen = None


class PostgresEventStore:
    """The :class:`~aksha.runtime.storage.ports.EventStore` over one PostgreSQL database."""

    def __init__(
        self,
        conn: psycopg.Connection[tuple[Any, ...]],
        dsn: str,
        clock: Clock,
        catalogue: Catalogue,
        *,
        boot: int,
        writer: bool,
        identity: StoreIdentity,
    ) -> None:
        """Wrap an open session; use :func:`open_store` rather than calling this."""
        self._conn: psycopg.Connection[tuple[Any, ...]] | None = conn
        self._dsn = dsn
        self._clock = clock
        self._catalogue = catalogue
        self._boot = boot
        self._writer = writer
        self._identity = identity
        self._head = _stored_head(conn, identity)
        """The writer's last committed ``(seq, hash)``; moves only after a commit."""
        self._mutex = threading.RLock()
        self._events = RowMemo(_event)
        """Decoded events by seq, reused only while the stored row is unchanged."""
        self._artifacts = RowMemo(_artifact)
        """Decoded artifact rows by (owner, ref), reused only while the row is unchanged."""

    @property
    def catalogue(self) -> Catalogue:
        """The event types this store accepts."""
        return self._catalogue

    @property
    def is_writer(self) -> bool:
        """Whether this session holds the writer's advisory lock."""
        return self._writer

    def _event(self, row: tuple[Any, ...]) -> Event:
        return self._events.get(row[0], row)

    def _artifact(self, row: tuple[Any, ...]) -> StoredArtifact:
        return self._artifacts.get((row[0], row[1]), row)

    def _db(self) -> psycopg.Connection[tuple[Any, ...]]:
        if self._conn is None:
            raise StoreError("store_closed", "this event store handle is closed")
        return self._conn

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._mutex:
            rows: list[tuple[Any, ...]] = self._db().execute(sql, params).fetchall()
        return rows

    def setting(self, name: str) -> str:
        """The session's value of a server setting (PostgreSQL only; for checks and tests)."""
        return str(self._query("SELECT current_setting(%s)", (name,))[0][0])

    def append(
        self,
        owner: str,
        boundary: str,
        events: Sequence[NewEvent],
        artifacts: Sequence[ArtifactRow] = (),
        *,
        expected_seq: int | None = None,
    ) -> tuple[Event, ...]:
        """Commit one boundary in one transaction, then notify subscribers; see the port."""
        if not self._writer:
            self._db()
            raise NotWriter()
        batch = prepare_append(self._catalogue, owner, boundary, events, artifacts)
        pg = driver()
        with self._mutex:
            db = self._db()
            try:
                with db.transaction():
                    stored, head_hash = self._insert(db, batch, expected_seq)
                self._head = (stored[-1].seq, head_hash)
            except pg.errors.UniqueViolation as e:
                self._refuse_recorded(batch)
                raise StoreError("constraint_violated", str(e)) from None
            except pg.Error as e:
                raise StoreError("backend_error", str(e)) from None
        return stored

    def _insert(
        self,
        db: psycopg.Connection[tuple[Any, ...]],
        batch: PreparedBatch,
        expected_seq: int | None,
    ) -> tuple[tuple[Event, ...], str]:
        """The body of one append transaction: the stored events and the new head's hash."""
        db.execute("SELECT pg_advisory_xact_lock(%s)", (WRITER_LOCK_KEY,))
        at = iso_utc(self._clock.now())  # inside the lock, so ``at`` follows seq
        if expected_seq is not None and (actual := self._last(batch.owner)) != expected_seq:
            raise SequenceConflict(batch.owner, expected_seq, actual)
        self._refuse_recorded(batch)
        stored_head = _stored_head(db, self._identity)
        if stored_head != self._head:
            raise StoreError(
                "chain_head_moved",
                f"the stored head is seq {stored_head[0]}, this writer last wrote"
                f" seq {self._head[0]}",
            )
        seq, prev = self._head
        stored: list[Event] = []
        for e in batch.events:
            seq += 1
            link = next_link(e.stored(seq=seq, owner=batch.owner, at=at, boot=self._boot), prev)
            db.execute(
                "INSERT INTO events (seq, owner, type, payload, run, instance, step, session,"
                " uniq, at, boot, schema_version, prev_hash, hash)"
                " VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (seq, batch.owner, e.type, e.payload_json, e.run, e.instance, e.step, e.session,
                 e.dedup_key, at, self._boot, e.schema_version, link.prev_hash, link.hash),
            )  # fmt: skip
            stored.append(link.event)
            prev = link.hash
        for index, a in batch.artifacts:
            db.execute(
                "INSERT INTO artifacts VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
                (batch.owner, a.ref, a.kind, a.key, a.version, canonical_json(a.content),
                 a.content_hash, a.canonical_hash, stored[index].seq),
            )  # fmt: skip
        db.execute("SELECT pg_notify(%s, %s)", (CHANNEL, batch.owner))
        return tuple(stored), prev

    def _refuse_recorded(self, batch: PreparedBatch) -> None:
        """Raise ``AlreadyRecorded`` if any of the batch's dedup keys is already stored."""
        keys = list(batch.dedup_keys())
        if not keys:
            return
        rows = self._query(
            f"SELECT {_EVENT_COLUMNS} FROM events WHERE owner = %s AND uniq = ANY(%s)",
            (batch.owner, keys),
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
        sql = f"SELECT {_EVENT_COLUMNS} FROM events WHERE owner = %s AND seq > %s"
        params: list[Any] = [check_owner(owner), after_seq]
        if upto_seq is not None:
            sql += " AND seq <= %s"
            params.append(upto_seq)
        if run is not None:
            sql += " AND run = %s"
            params.append(run)
        if types is not None:
            sql += " AND type = ANY(%s)"
            params.append(sorted(types))
        return iter([self._event(r) for r in self._query(sql + " ORDER BY seq", params)])

    def read_artifacts(
        self, owner: str, *, upto_seq: int | None = None
    ) -> tuple[StoredArtifact, ...]:
        """The owner's artifact rows saved at or before ``upto_seq``."""
        sql = f"SELECT {_ARTIFACT_COLUMNS} FROM artifacts WHERE owner = %s"
        params: list[Any] = [check_owner(owner)]
        if upto_seq is not None:
            sql += " AND event_seq <= %s"
            params.append(upto_seq)
        rows = self._query(sql + " ORDER BY event_seq, ref", params)
        return tuple(self._artifact(r) for r in rows)

    def _last(self, owner: str) -> int:
        rows = self._query("SELECT MAX(seq) FROM events WHERE owner = %s", (owner,))
        return int(rows[0][0] or 0)

    def last_seq(self, owner: str) -> int:
        """The owner's latest seq, 0 when it has none."""
        return self._last(check_owner(owner))

    def by_dedup_key(self, owner: str, key: str) -> Event | None:
        """The owner's event holding dedup key ``key``."""
        rows = self._query(
            f"SELECT {_EVENT_COLUMNS} FROM events WHERE owner = %s AND uniq = %s",
            (check_owner(owner), key),
        )
        return self._event(rows[0]) if rows else None

    def owners_with_open_runs(self) -> tuple[str, ...]:
        """Owners with a started run that has not ended, sorted."""
        rows = self._query(
            "SELECT DISTINCT s.owner FROM events s WHERE s.type = 'run.started' AND s.owner <> %s"
            " AND NOT EXISTS (SELECT 1 FROM events e WHERE e.owner = s.owner"
            " AND e.type = 'run.ended' AND e.run = s.run) ORDER BY s.owner",
            (SYSTEM_OWNER,),
        )
        return tuple(r[0] for r in rows)

    def owners_with_pending_timers(self) -> tuple[str, ...]:
        """Owners with a scheduled timer no ``tclose:`` key closed yet, sorted (ADR-P25 D1)."""
        rows = self._query(
            "SELECT DISTINCT s.owner FROM events s WHERE s.type = 'timer.scheduled'"
            " AND s.owner <> %s AND NOT EXISTS (SELECT 1 FROM events c"
            " WHERE c.owner = s.owner AND c.uniq = 'tclose:' || substr(s.uniq, 7))"
            " ORDER BY s.owner",
            (SYSTEM_OWNER,),
        )
        return tuple(r[0] for r in rows)

    def subscribe(self, owner: str, *, after_seq: int = 0) -> PostgresSubscription:
        """Follow the owner's events after ``after_seq`` on a listening connection."""
        check_owner(owner)
        self._db()
        return PostgresSubscription(self, owner, after_seq, self._dsn)

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
        links = (ChainLink(_event(r[:12]), str(r[12]), str(r[13])) for r in rows)
        return verify_chain(self._identity, links, anchors, artifacts=[_artifact(r) for r in saved])

    def close(self) -> None:
        """Close the session; for the writer that releases the advisory lock. Idempotent."""
        with self._mutex:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def open_store(
    dsn: str,
    clock: Clock,
    *,
    writer: bool,
    catalogue: Catalogue | None = None,
    store_id: str | None = None,
) -> PostgresEventStore:
    """Open the PostgreSQL event store in the database ``dsn`` names.

    ``writer=True`` takes the advisory lock first and raises ``LockHeld`` (exit 8) if
    another session holds it, then creates the schema (or checks the recorded schema
    version of an existing one) and records this process start in ``boots``.
    ``writer=False`` makes the session read-only and needs the schema to exist. Either
    raises ``StoreError("schema_mismatch")`` for a database of another shape, and
    ``encoding_unsupported`` for a database whose server encoding is not UTF8.
    ``catalogue`` defaults to the core types.

    A writer that creates the schema records the store's identity (``store_meta``):
    ``store_id``, or a random one, and the clock's time. Given for an existing store,
    ``store_id`` must be that store's (``store_id_mismatch`` otherwise).
    """
    catalogue = catalogue or Catalogue(CORE_EVENT_TYPES)
    if not writer:
        conn = _connect(dsn, application_name="aksha-reader")
        conn.execute("SET default_transaction_read_only = on")
        try:
            _require_utf8(conn)
            if not _has_table(conn, "events"):
                raise StoreError("store_missing", "no event store schema in this database")
            check_schema_version(_schema_version(conn))
            identity = _identity(conn)
            identity.check_id(store_id)
            store = PostgresEventStore(
                conn, dsn, clock, catalogue, boot=0, writer=False, identity=identity
            )
        except BaseException:
            conn.close()
            raise
        return store
    conn = _connect(dsn, application_name=f"{_WRITER_APP}{os.getpid()}")
    try:
        _require_utf8(conn)
        row = conn.execute("SELECT pg_try_advisory_lock(%s)", (WRITER_LOCK_KEY,)).fetchone()
        if not (row and row[0]):
            raise LockHeld(_lock_holder(conn))
        with conn.transaction():
            identity = _create_or_check(conn, iso_utc(clock.now()), store_id)
            booted = conn.execute(
                "INSERT INTO boots (pid, at) VALUES (%s, %s) RETURNING boot",
                (os.getpid(), iso_utc(clock.now())),
            ).fetchone()
        assert booted is not None
        return PostgresEventStore(
            conn, dsn, clock, catalogue, boot=int(booted[0]), writer=True, identity=identity
        )
    except BaseException:
        conn.close()
        raise
