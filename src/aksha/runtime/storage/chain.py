"""The event store's hash chain (ADR-P01 Amendment H): records, hashes, anchors and the walk.

Each stored event carries ``prev_hash`` (the hash of the event before it, or the store's
genesis hash for seq 1) and ``hash``, the SHA-256 of the canonical encoding of a record
holding the event's whole envelope and ``prev_hash`` (H2, H3). Changing, removing or
reordering a past event breaks the chain at that row, and :func:`verify_chain` finds the
first break. The chain is tamper-evident, not tamper-proof: someone who can write the
database can re-hash every later row, and only an :class:`Anchor` held outside the store
(H7) shows that, or a truncated tail.

Everything here is pure, so the SQLite and PostgreSQL adapters hash and verify with the
same code and produce the same bytes for the same events.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from aksha.runtime.canonical import NotCanonical, canonical_bytes, sha256_ref
from aksha.runtime.events.envelope import Event
from aksha.runtime.storage.errors import StoreError
from aksha.runtime.storage.rows import StoredArtifact

CHAIN_VERSION: Final = 1
"""The chain format version, the ``chain`` key of every hashed record (H2)."""

ChainReason = Literal[
    "hash_mismatch",
    "prev_hash_mismatch",
    "seq_gap",
    "genesis_mismatch",
    "anchor_mismatch",
    "truncated",
    "not_canonical",
    "artifact_mismatch",
]


def genesis_hash(store_id: str, created_at: str, chain_version: int = CHAIN_VERSION) -> str:
    """The hash seq 1 links to: over ``{"chain", "store_id", "created_at"}`` (H4)."""
    record = {"chain": chain_version, "store_id": store_id, "created_at": created_at}
    return sha256_ref(canonical_bytes(record))


def content_hash(content: Any) -> str:
    """An artifact version's ``content_hash``: ``sha256:`` over its canonical bytes (H3, H6).

    The same encoding as the chain, so a float or an integer beyond plus or minus 2**53
    in artifact content is refused (:class:`~aksha.runtime.canonical.NotCanonical`), as
    it is in a payload.
    """
    return sha256_ref(canonical_bytes(content))


def new_store_id() -> str:
    """A random store id, ``st_`` plus 32 hex characters, so two stores never share a chain."""
    return f"st_{secrets.token_hex(16)}"


@dataclass(frozen=True)
class StoreIdentity:
    """The one ``store_meta`` row: written when the store is created, never changed (H4)."""

    store_id: str
    created_at: str
    chain_version: int
    genesis_hash: str

    @classmethod
    def read(
        cls, store_id: str, created_at: str, chain_version: int, genesis: str
    ) -> StoreIdentity:
        """The identity a store recorded, refusing a chain format this code cannot verify.

        A store written by another chain version would be hashed here with version 1's
        rules and report a false ``hash_mismatch``, so it is refused on open instead
        (``chain_version_unsupported``).
        """
        if chain_version != CHAIN_VERSION:
            raise StoreError(
                "chain_version_unsupported",
                f"the store uses chain version {chain_version}; this runtime verifies"
                f" version {CHAIN_VERSION}",
            )
        return cls(store_id, created_at, chain_version, genesis)

    @classmethod
    def create(cls, store_id: str, created_at: str) -> StoreIdentity:
        """The identity of a new store, with its genesis hash computed."""
        if not store_id or "\x00" in store_id:
            raise StoreError("store_id_invalid", "a store id is a non-empty string without NUL")
        return cls(store_id, created_at, CHAIN_VERSION, genesis_hash(store_id, created_at))

    def check_id(self, store_id: str | None) -> None:
        """Refuse ``store_id`` when given and not this store's (``store_id_mismatch``)."""
        if store_id is not None and store_id != self.store_id:
            raise StoreError(
                "store_id_mismatch", f"this store is {self.store_id!r}, not {store_id!r}"
            )


def chain_record(event: Event, prev_hash: str) -> dict[str, Any]:
    """The record an event's hash covers (H2).

    It holds the chain version, ``prev_hash`` and every envelope field, so moving an event
    to another owner or run breaks the chain too, not only editing its payload.
    """
    return {
        "chain": CHAIN_VERSION,
        "seq": event.seq,
        "prev_hash": prev_hash,
        "type": event.type,
        "payload": event.payload,
        "at": event.at,
        "dedup_key": event.dedup_key,
        "owner": event.owner,
        "run": event.run,
        "instance": event.instance,
        "step": event.step,
        "session": event.session,
        "boot": event.boot,
        "schema_version": event.schema_version,
    }


def event_hash(event: Event, prev_hash: str) -> str:
    """``sha256:`` plus the hex SHA-256 of :func:`chain_record`'s canonical bytes. Pure."""
    return sha256_ref(canonical_bytes(chain_record(event, prev_hash)))


@dataclass(frozen=True)
class ChainLink:
    """A stored event with the two chain columns beside it."""

    event: Event
    prev_hash: str
    hash: str


@dataclass(frozen=True)
class UnreadableLink:
    """A stored row whose payload could not be decoded, with its chain columns.

    The walk still checks its seq and ``prev_hash``, then reports ``not_canonical`` there.
    """

    seq: int
    prev_hash: str
    hash: str
    why: str


@dataclass(frozen=True)
class UnreadableArtifact:
    """A stored artifact row whose content could not be decoded, or not canonically."""

    owner: str
    ref: str
    event_seq: int
    why: str


def next_link(event: Event, prev_hash: str) -> ChainLink:
    """The link the writer stores for ``event`` after a row whose hash is ``prev_hash``."""
    return ChainLink(event, prev_hash, event_hash(event, prev_hash))


@dataclass(frozen=True)
class ChainBreak:
    """The first place the stored chain disagrees with itself or with an anchor.

    ``seq`` is the first broken sequence number (0 for the genesis). ``expected`` is what
    the chain or the anchor says should be there; ``found`` is what the store holds.
    """

    seq: int
    reason: ChainReason
    expected: str | None
    found: str | None


@dataclass(frozen=True)
class Anchor:
    """A copy of one chain hash held outside the store, by a sink its writer cannot rewrite.

    ``sink`` names where it was published (``file:/mnt/audit/anchors.jsonl``); ``receipt``
    is the sink's own id for the write, when it gives one.
    """

    store_id: str
    seq: int
    hash: str
    at: str
    sink: str
    receipt: str | None = None


def verify_chain(
    identity: StoreIdentity,
    links: Iterable[ChainLink | UnreadableLink],
    anchors: Sequence[Anchor] = (),
    artifacts: Iterable[StoredArtifact | UnreadableArtifact] = (),
) -> ChainBreak | None:
    """Walk ``links`` in seq order from the genesis; return the first break, or None.

    For each row, in this order: seq must be the previous seq plus one (``seq_gap``),
    ``prev_hash`` must equal the previous row's hash (``prev_hash_mismatch``), the stored
    payload must decode (``not_canonical`` otherwise) and the recomputed hash must equal
    the stored one (``hash_mismatch``; ``not_canonical`` if the payload cannot be
    encoded). Then every artifact row saved by that event must match the event's
    ``ref``, ``kind``, ``key``, ``version`` and ``content_hash``, and its content must
    hash to that ``content_hash``, and an ``artifact.saved`` event with no row is a break
    too (``artifact_mismatch``, H6). Then each anchor, oldest
    first: an anchored seq past the head is ``truncated``, and a stored hash that differs
    is ``anchor_mismatch``. Without anchors a truncated tail, or a tail re-hashed by
    someone with write access, still verifies. An anchor of another store, or with a
    negative seq, is refused (``anchor_store_mismatch``, ``anchor_invalid``).
    """
    for anchor in anchors:
        if anchor.store_id != identity.store_id:
            raise StoreError(
                "anchor_store_mismatch",
                f"anchor for {anchor.store_id!r} checked against store {identity.store_id!r}",
            )
        if anchor.seq < 0:
            raise StoreError("anchor_invalid", f"an anchor's seq is 0 or more, not {anchor.seq}")
    genesis = genesis_hash(identity.store_id, identity.created_at, identity.chain_version)
    if genesis != identity.genesis_hash:
        return ChainBreak(0, "genesis_mismatch", genesis, identity.genesis_hash)
    saved_by: dict[int, list[StoredArtifact | UnreadableArtifact]] = {}
    for artifact in artifacts:
        saved_by.setdefault(artifact.event_seq, []).append(artifact)
    anchored = {a.seq for a in anchors}
    hashes: dict[int, str] = {0: identity.genesis_hash}
    seq, prev = 0, identity.genesis_hash
    for link in links:
        found = _link_break(link, seq + 1, prev)
        if found is not None:
            return _earliest(found, saved_by)
        assert isinstance(link, ChainLink)  # an unreadable link is always a break
        rows = saved_by.pop(link.event.seq, ())
        for artifact in rows:
            found = _artifact_break(link.event, artifact)
            if found is not None:
                return found
        if not rows and link.event.type == "artifact.saved":
            want = link.event.payload.get("content_hash")
            return ChainBreak(
                link.event.seq,
                "artifact_mismatch",
                None if want is None else str(want),
                "no artifact row for this event",
            )
        seq, prev = link.event.seq, link.hash
        if seq in anchored:
            hashes[seq] = prev
    if saved_by:  # rows pointing past the head, or at a seq the walk never reached
        orphan = min(saved_by)
        return ChainBreak(orphan, "artifact_mismatch", None, "no event at this seq")
    for anchor in sorted(anchors, key=lambda a: a.seq):
        if anchor.seq > seq:
            return ChainBreak(anchor.seq, "truncated", anchor.hash, None)
        if hashes[anchor.seq] != anchor.hash:
            return ChainBreak(anchor.seq, "anchor_mismatch", anchor.hash, hashes[anchor.seq])
    return None


def _earliest(
    found: ChainBreak, saved_by: dict[int, list[StoredArtifact | UnreadableArtifact]]
) -> ChainBreak:
    """``found``, unless an artifact row points at an earlier seq the walk never reached."""
    orphans = [s for s in saved_by if s < found.seq]
    if orphans:
        return ChainBreak(min(orphans), "artifact_mismatch", None, "no event at this seq")
    return found


def _link_break(link: ChainLink | UnreadableLink, seq: int, prev: str) -> ChainBreak | None:
    """The break at one row, given the seq and previous hash the chain expects there."""
    at = link.seq if isinstance(link, UnreadableLink) else link.event.seq
    if at != seq:
        return ChainBreak(at, "seq_gap", str(seq), str(at))
    if link.prev_hash != prev:
        return ChainBreak(at, "prev_hash_mismatch", prev, link.prev_hash)
    if isinstance(link, UnreadableLink):
        return ChainBreak(at, "not_canonical", None, link.why)
    try:
        recomputed = event_hash(link.event, link.prev_hash)
    except NotCanonical as e:
        return ChainBreak(at, "not_canonical", None, e.path)
    if recomputed != link.hash:
        return ChainBreak(at, "hash_mismatch", recomputed, link.hash)
    return None


def _artifact_break(
    event: Event, artifact: StoredArtifact | UnreadableArtifact
) -> ChainBreak | None:
    """The break, if any, between one artifact row and the ``artifact.saved`` event it names."""
    at = event.seq
    expected = event.payload.get("content_hash") if event.type == "artifact.saved" else None
    want = None if expected is None else str(expected)
    if isinstance(artifact, UnreadableArtifact):
        return ChainBreak(at, "artifact_mismatch", want, artifact.why)
    row = artifact.row
    if (
        want is None
        or artifact.owner != event.owner
        or (row.ref, row.kind, row.key, row.version)
        != tuple(event.payload.get(k) for k in ("ref", "kind", "key", "version"))
    ):
        return ChainBreak(
            at, "artifact_mismatch", want, f"row {row.ref!r} does not match its event"
        )
    if row.content_hash != want:
        return ChainBreak(at, "artifact_mismatch", want, row.content_hash)
    try:
        recomputed = content_hash(row.content)
    except NotCanonical as e:
        return ChainBreak(at, "artifact_mismatch", want, f"content not canonical at {e.path}")
    if recomputed != want:
        return ChainBreak(at, "artifact_mismatch", want, recomputed)
    return None
