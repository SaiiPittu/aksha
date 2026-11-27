"""Citation keying (ADR-P05 amendment M2): a version reopens when what its claims rest on moves.

Read keying, the behaviour before M2 and still the default, makes a saved version depend on
everything its step read: a change to any record it read reopens it, early cutoff then
stops the cascade, and the rebuild is paid anyway. Read lineage over-approximates. An
agent that declares ``Agent.invalidation_keys = "citations"`` (recorded on
``run.started{invalidation_keys}``; a run that records none reads as ``reads``) splits a
version's source reads into two kinds:

1. **Strong.** What the version's claims rest on, each changed only in the way named:

   a. a span the version cites (``citation.bound``, ADR-P13): its record moved and the
      span's text is no longer in the new version (``excerpts.rechecked`` says
      ``changed`` or ``gone``; a span no recheck covered since its record moved is taken
      as changed, failing closed);
   b. a scoped claim's scope (``grounding.claims``: an absence or coverage claim), and an
      explicit unknown's ``checked`` reads (an absence the runtime already knew): a scoped
      search whose hit set changed, or a scoped record that moved at all;
   c. a definition read (``definitions/<agent>/<alias>``, :mod:`invalidation.definitions`):
      the extraction schema or classification questions the value was produced under.

2. **Weak.** Every other record or query read that moved. The read is marked ``weak``
   in the invalidation view and does not invalidate the version: the change produces an
   ``evidence.noticed`` fact (a "new evidence may exist" notice with a low-priority
   recheck item), not a rebuild.

A version that cites nothing and declares no scope gives the runtime nothing to key on,
so every read of it stays strong: citation keying narrows read keying where the version
names its evidence, and never widens what it may miss. Artifact, statement and time reads
are never weak: they are the agent's own lineage, a person's answers and the clock.

``excerpts.rechecked`` is written by the one writer of source facts
(:func:`~aksha.runtime.invalidation.sources.record_source_changes`), in the source
change's transaction: for every span minted over a moved record, whether its text (with
whitespace runs collapsed, as the verifier compares quotes) is still in the record's new
text. Only a source that can hand back a record's text (``RecordTexts``) is compared; a
span of any other source is taken as changed.

Keying applies to agents in ``rebuild`` mode (the view's states) and ``notify`` mode (the
notices: a notify version never reads as stale, so it is noticed both for strong and for
weak changes). All pure.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, get_args

from aksha.runtime.artifacts.types import ArtifactVersion, InputRef
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.grounding.claims import claims_in, record_id
from aksha.runtime.grounding.types import STATEMENT_SOURCE, CitationSpan, SpanIndex, is_unknown_node
from aksha.runtime.grounding.verify import normalise
from aksha.runtime.projections.view import LogView

InvalidationKeys = Literal["reads", "citations"]
"""What a version's reads are keyed on: every read (``reads``) or its evidence (module doc)."""

INVALIDATION_KEYS: tuple[str, ...] = get_args(InvalidationKeys)

DEFAULT_KEYS: InvalidationKeys = "reads"
"""The keying of a definition that declares none: today's behaviour."""

UNRECORDED_KEYS: InvalidationKeys = "reads"
"""The keying of a run whose ``run.started`` records none: one written before M2."""

KEYS_FIELD = "invalidation_keys"
"""The ``run.started`` field that records the keying."""

DEFINITIONS_COLLECTION = "definitions"
"""The collection a definition read's record lives in: ``definitions/<agent>/<alias>``."""

EXCERPTS_RECHECKED = "excerpts.rechecked"
EVIDENCE_NOTICED = "evidence.noticed"

SpanCheck = Literal["kept", "changed", "gone"]
"""A cited span against its record's new version: still there, changed, or the record gone."""

Strength = Literal["strong", "weak"]

WEAK_NOTICE = "new evidence may exist"
STRONG_NOTICE = "cited evidence changed"


def run_keys(view: LogView) -> Mapping[str, InvalidationKeys]:
    """Each run in the log to the keying its ``run.started`` recorded (unrecorded: reads)."""
    out: dict[str, InvalidationKeys] = {}
    for e in view.events:
        if e.type == "run.started" and e.run is not None:
            value = e.payload.get(KEYS_FIELD, UNRECORDED_KEYS)
            out[e.run] = value if value in INVALIDATION_KEYS else UNRECORDED_KEYS
    return MappingProxyType(out)


def inherited_keys(view: LogView, for_runs: Iterable[str]) -> InvalidationKeys:
    """A rebuild run's keying: ``citations`` only when every run it rebuilds for keyed so."""
    keys = run_keys(view)
    runs = list(for_runs)
    if runs and all(keys.get(r) == "citations" for r in runs):
        return "citations"
    return "reads"


# --- what a version rests on ------------------------------------------------------------


@dataclass(frozen=True)
class Grounds:
    """What one version's claims rest on (module doc, item 1).

    ``spans`` are the span IDs it cites (statements left out: they are no record read),
    ``queries`` the recorded queries its scoped claims name, ``records`` the whole record
    IDs its scoped claims and explicit unknowns name.
    """

    spans: frozenset[str] = frozenset()
    queries: frozenset[str] = frozenset()
    records: frozenset[str] = frozenset()

    @property
    def named(self) -> bool:
        """Whether the version names any evidence; one that names none is read-keyed."""
        return bool(self.spans or self.queries or self.records)


def cited_spans(view: LogView) -> Mapping[str, frozenset[str]]:
    """Each carrier (a version ref) to the span IDs its ``citation.bound`` facts name."""
    out: dict[str, set[str]] = {}
    for e in view.events:
        if e.type == "citation.bound" and e.payload.get("form") == "span":
            out.setdefault(str(e.payload["carrier"]), set()).add(str(e.payload["target"]))
    return MappingProxyType({k: frozenset(v) for k, v in out.items()})


def grounds_of(
    version: ArtifactVersion, cited: Mapping[str, frozenset[str]], spans: SpanIndex
) -> Grounds:
    """What ``version`` rests on: its cited spans, its claims' scopes, its unknowns' reads."""
    ids = cited.get(str(version.ref), frozenset())
    record_spans = frozenset(
        i for i in ids if (s := spans.get(i)) is not None and s.source != STATEMENT_SOURCE
    )
    queries: set[str] = set()
    records: set[str] = set()
    for claim in claims_in(version.content):
        queries.update(claim.scope.queries)
        records.update(claim.scope.record_ids())
    for node in _unknowns(version.content):
        checked = node["unknown"].get("checked", ())
        for c in checked if isinstance(checked, list | tuple) else ():
            if str(c).startswith("record:"):
                records.add(record_id(str(c)))
            elif str(c).startswith("query:"):  # a search read in full: its hit set
                queries.add(str(c)[len("query:") :].rpartition("@")[0])
    return Grounds(record_spans, frozenset(queries), frozenset(records))


def _unknowns(value: Any) -> Iterable[Mapping[str, Any]]:
    if is_unknown_node(value) and isinstance(value["unknown"], Mapping):
        yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _unknowns(child)
    elif isinstance(value, list | tuple):
        for child in value:
            yield from _unknowns(child)


# --- span rechecks ----------------------------------------------------------------------


def span_checks(view: LogView) -> Mapping[str, tuple[SpanCheck, int]]:
    """Each span to its last recheck and the ``source_version`` it was made at."""
    out: dict[str, tuple[SpanCheck, int]] = {}
    for e in view.events:
        if e.type == EXCERPTS_RECHECKED:
            n = int(e.payload["source_version"])
            for span_id, state in e.payload["spans"].items():
                out[str(span_id)] = (state, n)
    return MappingProxyType(out)


def kept_spans(view: LogView) -> frozenset[str]:
    """The spans whose record moved and whose last recheck since then found them ``kept``.

    Such a span still quotes the record's current text, so a save may cite it (ADR-P13
    amendment M1); a span whose record moved with no recheck since stays refused.
    """
    moved: dict[str, int] = {}
    for e in view.events:
        if e.type == "source.changed":
            for rid in e.payload["records"]:
                moved[str(rid).partition("#")[0]] = int(e.payload["source_version"])
    checks = span_checks(view)
    out = {span_id for span_id, (state, _) in checks.items() if state == "kept"}
    if not out:
        return frozenset()
    index_records = _span_records(view, out)
    return frozenset(s for s in out if checks[s][1] >= moved.get(index_records.get(s, ""), 0))


def _span_records(view: LogView, ids: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in view.events:
        if e.type == "excerpts.minted":
            for s in e.payload["spans"]:
                if s["span_id"] in ids:
                    out[str(s["span_id"])] = str(s["record_id"])
    return out


def recheck(
    spans: SpanIndex,
    source: str,
    moved: Iterable[str],
    text: Callable[[str], str | None],
) -> dict[str, SpanCheck]:
    """Each span of ``source`` minted over a moved record, against the record's new text.

    ``moved`` names the moved records (or record parts); ``text`` returns a whole record's
    current text, None when it is gone. A span is ``kept`` when its text, whitespace
    collapsed, is still in the new text, ``changed`` when it is not, ``gone`` with the record.
    """
    bases = {rid.partition("#")[0] for rid in moved}
    out: dict[str, SpanCheck] = {}
    texts: dict[str, str | None] = {}
    for span in sorted(spans.spans.values(), key=lambda s: s.span_id):
        if span.source != source or span.record_id not in bases:
            continue
        if span.record_id not in texts:
            found = text(span.record_id)
            texts[span.record_id] = None if found is None else normalise(found)
        now = texts[span.record_id]
        if now is None:
            out[span.span_id] = "gone"
        else:
            out[span.span_id] = "kept" if normalise(span.text) in now else "changed"
    return out


def rechecked_event(number: int, checks: Mapping[str, SpanCheck]) -> NewEvent | None:
    """The ``excerpts.rechecked`` fact for one source change, or None when it checked none."""
    if not checks:
        return None
    return NewEvent(
        EXCERPTS_RECHECKED, {"source_version": number, "spans": dict(sorted(checks.items()))}
    )


# --- strong or weak ---------------------------------------------------------------------


@dataclass(frozen=True)
class Keyed:
    """What citation keying reads from the log, once per view."""

    keys: Mapping[str, InvalidationKeys]
    cited: Mapping[str, frozenset[str]]
    spans: SpanIndex
    checks: Mapping[str, tuple[SpanCheck, int]]
    moved_at: Mapping[str, int]
    """Each record to the ``source_version`` that last moved it."""

    @classmethod
    def of(cls, view: LogView, spans: SpanIndex) -> Keyed:
        """Read the keying facts of ``view`` (``spans`` is its span index)."""
        moved: dict[str, int] = {}
        for e in view.events:
            if e.type == "source.changed":
                for rid in e.payload["records"]:
                    moved[str(rid).partition("#")[0]] = int(e.payload["source_version"])
        return cls(run_keys(view), cited_spans(view), spans, span_checks(view),
                   MappingProxyType(moved))  # fmt: skip

    def keyed_on_citations(self, version: ArtifactVersion) -> bool:
        """Whether ``version``'s run keys on citations (module doc)."""
        return version.run is not None and self.keys.get(version.run) == "citations"

    def grounds(self, version: ArtifactVersion) -> Grounds:
        """What ``version`` rests on."""
        return grounds_of(version, self.cited, self.spans)

    def strength(self, version: ArtifactVersion, read: InputRef) -> Strength:
        """Whether a moved or gone ``read`` of ``version`` is strong or weak (module doc)."""
        if not self.keyed_on_citations(version) or read.kind not in ("record", "query"):
            return "strong"
        grounds = self.grounds(version)
        if not grounds.named:
            return "strong"
        if read.kind == "query":
            return "strong" if read.id in grounds.queries else "weak"
        base = read.id.partition("#")[0]
        if base.split("/", 1)[0] == DEFINITIONS_COLLECTION or base in grounds.records:
            return "strong"
        for span_id in grounds.spans:
            span = self.spans.get(span_id)
            if span is not None and span.record_id == base and self._span_changed(span):
                return "strong"
        return "weak"

    def why(self, version: ArtifactVersion, read: InputRef) -> str:
        """Why a moved ``read`` of ``version`` is strong or weak, in a few words."""
        if not self.keyed_on_citations(version):
            return "read-keyed"
        if read.kind not in ("record", "query"):
            return f"{read.kind} lineage"
        grounds = self.grounds(version)
        if not grounds.named:
            return "cites nothing, so read-keyed"
        base = read.id.partition("#")[0]
        if read.kind == "query":
            return "scoped search" if read.id in grounds.queries else "uncited search"
        if base.split("/", 1)[0] == DEFINITIONS_COLLECTION:
            return "definition"
        if base in grounds.records:
            return "scoped record"
        return "cited span" if self.strength(version, read) == "strong" else "uncited text"

    def _span_changed(self, span: CitationSpan) -> bool:
        moved = self.moved_at.get(span.record_id)
        if moved is None:
            return False
        check = self.checks.get(span.span_id)
        if check is None or check[1] < moved:
            return True  # no recheck since the record last moved: fail closed
        return check[0] != "kept"


# --- notices ----------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceNotice:
    """One ``evidence.noticed`` fact read back: a version, what moved, and how strongly."""

    version: str
    strength: Strength
    reads: tuple[InputRef, ...]
    source_version: int
    seq: int


def notices(view: LogView) -> tuple[EvidenceNotice, ...]:
    """Every ``evidence.noticed`` fact, in log order."""
    out = []
    for e in view.events:
        if e.type == EVIDENCE_NOTICED:
            p = e.payload
            reads = tuple(InputRef.from_json(r) for r in p["reads"])
            out.append(EvidenceNotice(str(p["version"]), p["strength"], reads,
                                      int(p["source_version"]), e.seq))  # fmt: skip
    return tuple(out)


def recheck_items(view: LogView) -> tuple[EvidenceNotice, ...]:
    """The open low-priority recheck items: weak notices whose version is still the latest.

    A notice is closed once a newer version of its slot is saved after it.
    """
    saved: dict[str, int] = {}
    for e in view.events:
        if e.type == "artifact.saved":
            slot = str(e.payload["ref"]).rpartition("@")[0]
            saved[slot] = e.seq
    return tuple(
        n
        for n in notices(view)
        if n.strength == "weak" and saved.get(n.version.rpartition("@")[0], 0) <= n.seq
    )


def notice_event(
    version: str, strength: Strength, reads: Iterable[InputRef], number: int, run: str | None
) -> NewEvent:
    """The ``evidence.noticed`` fact: a weak change asks for a low-priority recheck."""
    listed = sorted(set(reads), key=InputRef.sort_key)
    what = ", ".join(r.id for r in listed)
    text = WEAK_NOTICE if strength == "weak" else STRONG_NOTICE
    payload = {
        "version": version,
        "strength": strength,
        "reads": [r.as_json() for r in listed],
        "source_version": number,
        "notice": f"{text}: {what} changed since {version} was saved",
        "recheck": "low" if strength == "weak" else "high",
    }
    return NewEvent(EVIDENCE_NOTICED, payload, run=run)
