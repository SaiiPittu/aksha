"""Protocols for storage: ``EventStore``, ``Subscription`` and ``AnchorSink``.

``EventStore`` is ADR-P01's interface, plus the two things a second backend needs said
out loud: an optimistic ``expected_seq`` on ``append``, and ``subscribe`` for readers that
follow new commits. The rules every adapter keeps, checked by ``tests/contract/storage``:

1. Append-only: the database itself refuses ``UPDATE`` and ``DELETE`` on both tables.
2. One writer, under a lock the operating system or the server releases however the
   process dies. Readers never take it and cannot append.
3. A boundary is one transaction. On a dedup-key conflict nothing in it commits.
4. Dedup keys (v2: uniqueness tokens) are unique per owner, under a unique index.
5. Every read takes the owner first and returns only that owner's facts.
6. ``seq`` is global and assigned by the writer as the last seq plus one, so it has no
   gaps: a gap reads as a deleted row (ADR-P01 H5).
7. Hash-chained (ADR-P01 Amendment H): every row stores ``prev_hash`` and ``hash``,
   computed by :mod:`aksha.runtime.storage.chain` inside the append transaction, under
   the single writer. ``verify()`` walks the chain; both backends produce the same hashes.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Protocol

from aksha.runtime.events.catalogue import Catalogue
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.storage.chain import Anchor, ChainBreak, StoreIdentity
from aksha.runtime.storage.rows import ArtifactRow, StoredArtifact


class AnchorSink(Protocol):
    """Somewhere outside the store that keeps copies of chain hashes (ADR-P01 H7).

    It is only worth having if the store's writer cannot rewrite it: another machine, a
    write-once (WORM) bucket under object lock, a signed commit in another repository.
    """

    @property
    def name(self) -> str:
        """Where anchors go, e.g. ``file:/mnt/audit/anchors.jsonl``; recorded in each one."""
        ...

    def publish(self, anchor: Anchor) -> Anchor:
        """Keep ``anchor`` and return it as stored (``sink`` and ``receipt`` filled).

        Idempotent on ``(store_id, seq)``: publishing the same hash again returns the
        stored anchor; a different hash for a seq already anchored is refused.
        """
        ...

    def read(self, store_id: str) -> Sequence[Anchor]:
        """Every anchor kept for ``store_id``, oldest (lowest seq) first."""
        ...


class Subscription(Protocol):
    """A cursor over one owner's events that can wait for the next commit."""

    @property
    def cursor(self) -> int:
        """The seq of the last event this subscription returned (its starting point before)."""
        ...

    def wait(self, timeout: float) -> tuple[Event, ...]:
        """Return the owner's events after the cursor, waiting up to ``timeout`` seconds.

        Returns at once when events are already there; returns ``()`` on timeout. Advances
        the cursor past what it returns.
        """
        ...

    def close(self) -> None:
        """Release what the subscription holds (a connection, a listener). Idempotent."""
        ...


class EventStore(Protocol):
    """The only authority (ADR-P01 D2): an append-only log of facts, read by owner."""

    @property
    def catalogue(self) -> Catalogue:
        """The event types this store accepts on append."""
        ...

    @property
    def is_writer(self) -> bool:
        """Whether this handle holds the writer lock."""
        ...

    def append(
        self,
        owner: str,
        boundary: str,
        events: Sequence[NewEvent],
        artifacts: Sequence[ArtifactRow] = (),
        *,
        expected_seq: int | None = None,
    ) -> tuple[Event, ...]:
        """Commit one transaction boundary, all or nothing; writer only.

        Validates every payload against its spec, checks each type may appear in
        ``boundary``, renders dedup keys, fills ``seq``, ``at``, ``boot`` and
        ``schema_version``, and chains each event to the one before it. Refuses with
        ``chain_head_moved`` if the stored head is not the one this writer last wrote.
        With ``expected_seq``, refuses with ``SequenceConflict`` unless the owner's last
        seq still equals it. On a dedup-key conflict raises
        ``AlreadyRecorded`` with the first existing event and commits nothing.
        """
        ...

    def read(
        self,
        owner: str,
        *,
        after_seq: int = 0,
        upto_seq: int | None = None,
        run: str | None = None,
        types: frozenset[str] | None = None,
    ) -> Iterator[Event]:
        """The owner's events with ``after_seq < seq <= upto_seq``, in seq order.

        ``after_seq`` is the cursor: pass the last seq already seen.
        """
        ...

    def read_artifacts(
        self, owner: str, *, upto_seq: int | None = None
    ) -> tuple[StoredArtifact, ...]:
        """The owner's artifact rows whose saved event is at or before ``upto_seq``."""
        ...

    def last_seq(self, owner: str) -> int:
        """The seq of the owner's latest event, 0 when there is none."""
        ...

    def by_dedup_key(self, owner: str, key: str) -> Event | None:
        """The owner's event holding ``key``, if any."""
        ...

    def owners_with_open_runs(self) -> tuple[str, ...]:
        """Owners with a ``run.started`` and no ``run.ended`` for it, sorted; recovery only."""
        ...

    def subscribe(self, owner: str, *, after_seq: int = 0) -> Subscription:
        """A subscription to the owner's events after ``after_seq``."""
        ...

    def identity(self) -> StoreIdentity:
        """The store's id, creation time and genesis hash, written once when it was created."""
        ...

    def head(self) -> tuple[int, str]:
        """The last seq and its hash across all owners; ``(0, genesis_hash)`` when empty."""
        ...

    def verify(self, *, anchors: Sequence[Anchor] = ()) -> ChainBreak | None:
        """Walk the whole chain, all owners, in seq order; return the first break or None.

        Recomputes every hash, checks each ``prev_hash`` and that seq has no gaps, checks
        each artifact row against its ``artifact.saved`` event's ``content_hash`` (H6),
        then checks each anchor (:func:`aksha.runtime.storage.chain.verify_chain`). A row
        the adapter cannot decode is reported (``not_canonical``), never raised. Works on a
        reader. Operator use only, like ``owners_with_open_runs``; never per owner.
        """
        ...

    def close(self) -> None:
        """Release the connection and, for the writer, the lock. Idempotent."""
        ...
