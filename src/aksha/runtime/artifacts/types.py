"""Artifact values: kinds and relations agents declare, refs, lineage inputs and versions.

ADR-P04. An agent declares ``ArtifactKind`` and ``RelationKind``; a tool returns a
``SaveRequest`` (with ``RelationRequest`` values, each carrying ``Excerpt`` evidence); the
store answers with an ``ArtifactVersion`` and the facts to commit (``Saved``). A version
is named by its ``ArtifactRef`` (``notes.line/n1@v2``); a lineage input is an
``InputRef``. ``ArtifactKind.cites`` holds the kind's per-field cite policies (ADR-P13 D4);
its ``validate`` and ``choice`` fields wait for their value types (ADR-P04 S1).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from aksha.runtime.canonical import freeze
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.grounding.types import CitePolicy
from aksha.runtime.storage.rows import ArtifactRow
from aksha.runtime.tasks.types import Check

InputKind = Literal["record", "query", "statement", "artifact", "blob", "time"]
"""What a lineage input is: a source record, a recorded query, a person's statement, an
artifact version, a blob, or a recorded time read (ADR-P25 D4: ``time:clock@<ISO date>``)."""

INPUT_KINDS: tuple[InputKind, ...] = (
    "record",
    "query",
    "statement",
    "artifact",
    "blob",
    "time",
)

Origin = Literal["task", "derived", "choice", "copy_choice"]
"""Who made a version: a Task instance's save, a derived kind, or a person's choice."""

PROVENANCE_RELATIONS: frozenset[str] = frozenset({"derived_from", "supersedes"})
"""The two relations only the runtime writes (ADR-P04 D6); no kind or request may use them."""

_REF = re.compile(r"^(?P<kind>[^/@\s]+)(?:/(?P<key>[^@\s]+))?@v(?P<version>[1-9][0-9]*)$")


@dataclass(frozen=True)
class ArtifactKind:
    """A kind of artifact: its schema and how the store treats its versions.

    ``name`` is local (``line``); the loader prefixes the declarer's namespace. ``schema`` is
    a JSON Schema mapping or a dataclass. ``derived`` is a pure function over other kinds.
    ``canon_exclude`` lists JSON pointers left out of the canonical form, ``list_keys``
    maps an array's pointer to the field its items are sorted by, and ``canonicalize`` is
    a pure function applied before both (ADR-P04 D5). ``cites`` lists the cite policies of
    its grounded fields (ADR-P13 D4): each matched field cites its evidence or, where the
    policy allows, holds an explicit unknown. ``max_age_s`` is its freshness lifetime
    (ADR-P25 D3): each save schedules a recheck of the version saved, and once it fires the
    version is out of date and is rebuilt like any invalidated one. ``compute`` is a pure
    function from the content saved to the content stored (ADR-P04 K1): the fields it
    writes are computed by code, never taken from the model (a count, a total, a phrase
    built from them), and every save of the kind, a correction's too, applies it first.
    ``compute_reads`` names the kinds (local, or ``ns.kind``; unkeyed) whose current content
    a ``compute`` needs as well (K1e): when it names any, ``compute`` is called with the
    content and a mapping from each name to that kind's current content (None when unsaved).
    """

    name: str
    schema: type | Mapping[str, Any]
    keyed: bool = False
    reviewable: bool = False
    lifecycle: bool = False
    derived: Callable[..., Mapping[str, Any]] | None = None
    readiness: tuple[Check, ...] = ()
    writers: Literal["one", "chain"] = "one"
    canon_exclude: tuple[str, ...] = ()
    list_keys: Mapping[str, str] = field(default_factory=dict)
    canonicalize: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None
    save: Literal["any", "tool_only"] = "any"
    summary: str | None = None
    cites: tuple[CitePolicy, ...] = ()
    max_age_s: int | None = None
    compute: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None
    compute_reads: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationKind:
    """A typed relation between artifacts or records (v2 ``EdgeKind``).

    Each end is a kind (local, or ``ns.kind``) or ``record:<collection>``. ``labels``, when
    set, is the closed list a saved relation's label is taken from. ``cites="per_end"``
    asks for evidence from each end rather than one excerpt in all.
    """

    name: str
    src: tuple[str, ...]
    dst: tuple[str, ...]
    labels: tuple[str, ...] = ()
    symmetric: bool = False
    transitive: bool = False
    cites: Literal["one", "per_end"] = "one"


@dataclass(frozen=True)
class ArtifactRef:
    """The name of one artifact version: kind, key (None for an unkeyed kind), version."""

    kind: str
    key: str | None
    version: int

    def __post_init__(self) -> None:
        """Refuse a version below 1 and a kind or key the ref grammar cannot hold."""
        if self.version < 1:
            raise ValueError(f"an artifact version starts at 1, not {self.version}")
        if _REF.match(str(self)) is None:
            raise ValueError(f"not an artifact ref: {self.kind!r}, {self.key!r}")

    def slot(self) -> tuple[str, str | None]:
        """The line of versions this one belongs to: ``(kind, key)``."""
        return (self.kind, self.key)

    def slot_id(self) -> str:
        """The slot as text: ``kind/key``, or ``kind`` for an unkeyed kind."""
        return self.kind if self.key is None else f"{self.kind}/{self.key}"

    def as_input(self) -> InputRef:
        """This version as a lineage input."""
        return InputRef("artifact", self.slot_id(), f"v{self.version}")

    def __str__(self) -> str:
        """``kind/key@vN``, or ``kind@vN`` for an unkeyed kind."""
        return f"{self.slot_id()}@v{self.version}"

    @classmethod
    def parse(cls, text: str) -> ArtifactRef:
        """Read ``kind/key@vN`` or ``kind@vN`` back (``ValueError`` for anything else)."""
        m = _REF.match(text)
        if m is None:
            raise ValueError(f"not an artifact ref: {text!r}")
        return cls(m["kind"], m["key"], int(m["version"]))

    @classmethod
    def from_input(cls, read: InputRef) -> ArtifactRef:
        """The version an ``artifact`` lineage input names (``ValueError`` for another kind)."""
        if read.kind != "artifact":
            raise ValueError(f"a {read.kind} input names no artifact version")
        return cls.parse(f"{read.id}@{read.version}")


@dataclass(frozen=True)
class InputRef:
    """One lineage input, and one ``derived_from`` edge (ADR-P04 D6).

    ``id`` names the input (``notes/N-0004``, ``Q-0007``, ``notes.line/n1``) and ``version``
    its version (a record version, a result-set hash, ``v2``). ``path`` and ``entry_hash``
    (S3) narrow the read to one entry of a version; they are set together or not at all.
    """

    kind: InputKind
    id: str
    version: str
    path: str | None = None
    entry_hash: str | None = None

    def __post_init__(self) -> None:
        """Refuse an unknown kind, an empty id or version, and a path without its hash."""
        if self.kind not in INPUT_KINDS:
            raise ValueError(f"an input is one of {INPUT_KINDS}, not {self.kind!r}")
        if not self.id or not self.version:
            raise ValueError("an input names an id and a version")
        if (self.path is None) != (self.entry_hash is None):
            raise ValueError("a path-scoped read names its path and its entry hash together")
        if self.kind == "artifact":
            ArtifactRef.parse(f"{self.id}@{self.version}")

    def sort_key(self) -> tuple[str, str, str, str, str]:
        """The order lineage is kept in: kind, id, version, path, entry hash."""
        return (self.kind, self.id, self.version, self.path or "", self.entry_hash or "")

    def same_source(self, other: InputRef) -> bool:
        """Whether both name the same input, ignoring the version and the path."""
        return (self.kind, self.id) == (other.kind, other.id)

    def as_json(self) -> dict[str, str]:
        """The input as the events record it; absent optional fields are left out."""
        out = {"kind": self.kind, "id": self.id, "version": self.version}
        if self.path is not None and self.entry_hash is not None:
            out |= {"path": self.path, "entry_hash": self.entry_hash}
        return out

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> InputRef:
        """Read :meth:`as_json`'s form back."""
        return cls(
            value["kind"], value["id"], value["version"], value.get("path"), value.get("entry_hash")
        )

    def __str__(self) -> str:
        """``kind:id@version``, with ``#path`` for a path-scoped read."""
        text = f"{self.kind}:{self.id}@{self.version}"
        return text if self.path is None else f"{text}#{self.path}"


def sorted_inputs(reads: Any) -> tuple[InputRef, ...]:
    """``reads`` deduplicated and in :meth:`InputRef.sort_key` order."""
    return tuple(sorted(set(reads), key=InputRef.sort_key))


@dataclass(frozen=True)
class WriterId:
    """The Task instance that owns a slot and regenerates it: agent, Task path, instance key."""

    agent: str
    task_path: str
    key: str | None = None

    def as_json(self) -> dict[str, str | None]:
        """The writer as ``artifact.saved`` records it."""
        return {"agent": self.agent, "task_path": self.task_path, "key": self.key}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> WriterId:
        """Read :meth:`as_json`'s form back."""
        return cls(value["agent"], value["task_path"], value.get("key"))


@dataclass(frozen=True)
class Excerpt:
    """Evidence for a relation: a quote from one input, at a JSON pointer in it.

    For an artifact source the store checks the quote is in the string at ``pointer``
    (anywhere in the content when ``pointer`` is ``""``). Any other source is cited through
    ``span``, a citation span minted over that input's text, and the quote is verified
    against it like any citation (ADR-P13 D2, decision 9).
    """

    source: InputRef
    quote: str
    pointer: str = ""
    span: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The excerpt as ``relation.saved`` records it; ``span`` only when set."""
        out = {"source": self.source.as_json(), "quote": self.quote, "pointer": self.pointer}
        if self.span is not None:
            out["span"] = self.span
        return out

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Excerpt:
        """Read :meth:`as_json`'s form back."""
        return cls(
            InputRef.from_json(value["source"]),
            value["quote"],
            value.get("pointer", ""),
            value.get("span"),
        )


SELF: Literal["self"] = "self"
"""A relation end that is the version being saved: the relation's carrier."""

End = InputRef | Literal["self"]
"""One end of a requested relation: an input, or the carrier itself."""


@dataclass(frozen=True)
class RelationRequest:
    """A typed relation a save asks for, with the evidence that supports it.

    ``kind`` is a full or local relation name. ``src`` and ``dst`` are inputs (an artifact
    version or a record) or :data:`SELF`. The relation is saved with the version it rides
    on (its carrier) and is current exactly while that version is.
    """

    kind: str
    src: End
    dst: End
    evidence: tuple[Excerpt, ...] = ()
    label: str | None = None


@dataclass(frozen=True)
class SaveRequest:
    """A save a tool asks for; the artifact store does the save and records its reads.

    ``kind`` is a full or local kind name; ``key`` is None for an unkeyed kind.
    ``primary_inputs`` labels some of the instance's reads for display only: lineage is
    always every captured read (ADR-P04 D6). ``relations`` ride on the saved version.
    ``supersedes`` names the slot's latest version this save replaces for every reader
    (ADR-P09 D4, D11): the store commits a ``supersedes`` relation from the new version to
    it in the same transaction, and the invalidation view then counts the new version for
    readers of every run, not only the saving run's line (ADR-P05 S1). ``answer`` names the
    interrupt whose answer the runtime saved this from (a kind a declared choice saves);
    no tool sets it.
    """

    kind: str
    key: str | None
    content: Mapping[str, Any]
    primary_inputs: tuple[InputRef, ...] = ()
    relations: tuple[RelationRequest, ...] = ()
    supersedes: str | None = None
    answer: str | None = None


@dataclass(frozen=True)
class ArtifactVersion:
    """One immutable saved version, as the store reads it back from the event store."""

    owner: str
    ref: ArtifactRef
    content: Mapping[str, Any]
    content_hash: str
    canonical_hash: str
    reads: tuple[InputRef, ...]
    reads_hash: str
    writer: WriterId
    origin: Origin
    run: str | None
    step: str | None
    event_seq: int
    primary_inputs: tuple[InputRef, ...] = ()
    answer: str | None = None
    """The answered choice the runtime saved this version from, if it did."""

    def __post_init__(self) -> None:
        """Deep-freeze ``content``."""
        object.__setattr__(self, "content", freeze(self.content))


@dataclass(frozen=True)
class ArtifactSnapshot:
    """One version as a read tool sees it through ``ToolContext.artifacts`` (ADR-P09 D3, D5).

    ``freshness`` is the version's state in the invalidation view (``current``,
    ``current_by_cutoff``, ``dirty`` or ``invalidated``, ADR-P05 D4) as the log stood before
    the reading step started. ``is_latest`` says no newer version of its slot exists in any
    run, a pending revision nobody accepted aside, so a tool can tell that something it
    cites has moved on even where the sibling rule keeps its readers current.
    """

    ref: ArtifactRef
    content: Mapping[str, Any]
    writer: WriterId
    run: str | None
    freshness: str
    is_latest: bool

    def __post_init__(self) -> None:
        """Deep-freeze ``content``."""
        object.__setattr__(self, "content", freeze(self.content))


@dataclass(frozen=True)
class Relation:
    """A saved typed relation: its kind, label, ends, evidence and carrier version."""

    kind: str
    label: str | None
    src: InputRef
    dst: InputRef
    evidence: tuple[Excerpt, ...]
    carrier: ArtifactRef
    event_seq: int


@dataclass(frozen=True)
class Saved:
    """What a save returns: the version and the facts that commit with the step's result.

    ``events`` and ``rows`` go into the step's ``StepOutput`` and commit with its
    ``step.completed`` in one transaction (U2). An idempotent save (the slot's latest
    version, equal in content, reads and relations) is ``replayed`` and commits nothing; an
    equal version adopted from another writer commits only the read of it.
    """

    version: ArtifactVersion
    events: tuple[NewEvent, ...] = ()
    rows: tuple[ArtifactRow, ...] = ()
    replayed: bool = False


@dataclass(frozen=True)
class LineageGraph:
    """The lineage around one version: every node reached and every ``derived_from`` edge.

    An edge ``(a, b)`` reads "``b`` was derived from ``a``". Nodes are lineage inputs; an
    artifact version is the input :meth:`ArtifactRef.as_input` gives.
    """

    root: InputRef
    nodes: frozenset[InputRef]
    edges: frozenset[tuple[InputRef, InputRef]]
