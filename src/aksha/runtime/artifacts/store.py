"""``ArtifactStore``: the one save path (ADR-P04 D7) and the readers over it.

``save`` checks a ``SaveRequest`` and returns the facts to commit (``Saved``); it never
appends. The step that ran the tool puts ``Saved.events`` and ``Saved.rows`` into its
``StepOutput``, so the artifact row, ``artifact.saved``, every ``relation.saved`` and the
step's ``step.completed`` commit in one transaction (U2), and a retried step replays the
recorded result instead of saving twice. The checks, in order, each refusing with a
``SaveRefused`` code and the pointer of the part to fix:

1. The kind is declared (``kind_unknown``) and the Task writes it (``rights``); a keyed
   kind has a key and an unkeyed one has none (``key_required``, ``key_not_allowed``).
2. The content is in the chain's encoding (``not_canonical_content``) and matches the
   kind's schema (``schema``).
3. Lineage inputs: every read the instance recorded, never a subset (D6). Every primary
   input and every relation end and evidence source must be one of them
   (``read_not_seen``).
4. Every read current (``read_invalidated``): an artifact read of a version this agent
   wrote must be its slot's current version (the latest that is not a pending revision,
   ADR-P05 D1), or, for a path-scoped read (S3), the current version's entry at that path
   must hash the same. A read of another agent's version is
   pinned (D11) and stays current; only invalidation (ADR-P05) can make it stale.
5. Relations: never ``derived_from`` or ``supersedes`` (``provenance_relation_forbidden``),
   a declared kind (``relation_undeclared``), a declared label (``relation_label_invalid``),
   ends of the declared kinds that exist (``relation_end_invalid``), and evidence: at
   least one excerpt, one per end for ``cites="per_end"`` (``citation_missing``), each
   quote found where it points in an artifact source (``citation_invalid``).
6. One writer per slot (D4): another writer is refused ``foreign_writer``, unless its
   content equals the latest version, which it adopts as a read. A ``writers="chain"`` kind
   lets every instance of one declared Task write the slot in turn (S4).
7. Idempotency against the slot's latest version only (D8): equal content, reads and
   relations return it and commit nothing. A reverted change is a new version.

Phrase rules, validators (S1), derived kinds (D9), lifecycle and choice bindings are not
built; nor is a kind's ``save="tool_only"``, which needs the tool's name on the request.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Collection, Iterable, Mapping
from typing import Any, Literal

from jsonschema import Draft202012Validator

from aksha.runtime.artifacts import lineage as _lineage
from aksha.runtime.artifacts import relations as _relations
from aksha.runtime.artifacts import versions as _versions
from aksha.runtime.artifacts.errors import BlobError, SaveRefused
from aksha.runtime.artifacts.hashing import (
    canonical_hash,
    content_hash,
    entry_hash,
    reads_hash,
    resolve,
)
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import (
    PROVENANCE_RELATIONS,
    SELF,
    ArtifactKind,
    ArtifactRef,
    ArtifactVersion,
    End,
    Excerpt,
    InputRef,
    LineageGraph,
    Origin,
    Relation,
    RelationKind,
    RelationRequest,
    Saved,
    SaveRequest,
    WriterId,
    sorted_inputs,
)
from aksha.runtime.canonical import NotCanonical, nul_path
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.projections.view import LogView
from aksha.runtime.storage.rows import ArtifactRow

_Resolved = tuple[str, str | None, InputRef, InputRef, tuple[Excerpt, ...]]
"""A checked relation: full kind, label, src, dst, evidence."""


def _resolve_name(name: str, namespace: str) -> str:
    """A full name: ``name`` if it has a dot, else ``namespace.name`` (as the loader does)."""
    return name if "." in name or name.startswith("record:") else f"{namespace}.{name}"


def _json_pointer(parts: Iterable[str | int]) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


def _whole(read: InputRef) -> InputRef:
    return read if read.path is None else InputRef(read.kind, read.id, read.version)


class ArtifactStore:
    """The save path and the version, lineage and relation readers for one definition.

    ``kinds`` and ``relations`` are keyed by full name (``LoadedDefinition.kinds``).
    ``blobs`` is where offloaded payload values live; without it a reader refuses a
    payload holding a stub (``blob_store_required``).
    """

    def __init__(
        self,
        kinds: Mapping[str, ArtifactKind],
        relations: Mapping[str, RelationKind] | None = None,
        *,
        blobs: BlobStore | None = None,
    ) -> None:
        """Hold the declared kinds and relations and the blob store."""
        self._kinds = dict(kinds)
        self._relations = dict(relations or {})
        self._blobs = blobs

    # --- readers ---------------------------------------------------------------------

    def latest(self, view: LogView, kind: str, key: str | None) -> ArtifactVersion | None:
        """The slot's newest version."""
        return _versions.latest(view, kind, key, blobs=self._blobs)

    def current(self, view: LogView, kind: str, key: str | None) -> ArtifactVersion | None:
        """The latest version that is not an unaccepted pending revision."""
        return _versions.current(view, kind, key, blobs=self._blobs)

    def version(self, view: LogView, ref: ArtifactRef) -> ArtifactVersion | None:
        """The version ``ref`` names."""
        return _versions.version(view, ref, blobs=self._blobs)

    def versions(self, view: LogView, kind: str, key: str | None) -> tuple[ArtifactVersion, ...]:
        """The slot's versions, oldest first."""
        return _versions.versions(view, kind, key, blobs=self._blobs)

    def lineage(
        self,
        view: LogView,
        ref: ArtifactRef,
        *,
        direction: Literal["up", "down"] = "up",
        depth: int | None = None,
    ) -> LineageGraph:
        """What ``ref`` was built from (``up``) or what was built from it (``down``)."""
        return _lineage.lineage(view, ref, direction=direction, depth=depth, blobs=self._blobs)

    def relations(
        self,
        view: LogView,
        *,
        kind: str | None = None,
        touching: ArtifactRef | InputRef | None = None,
    ) -> tuple[Relation, ...]:
        """The current relations, by kind or by an end."""
        return _relations.relations(view, kind=kind, touching=touching, blobs=self._blobs)

    def components(self, view: LogView, kind: str) -> tuple[frozenset[InputRef], ...]:
        """The groups of ends the current relations of ``kind`` join (S2)."""
        return _relations.components(view, kind, blobs=self._blobs)

    def put_blob(self, data: bytes) -> str:
        """Store ``data`` in the blob store; its ref, ``sha256:<hex>``."""
        return self._need_blobs().put(data)

    def get_blob(self, ref: str) -> bytes:
        """The bytes named ``ref``, re-hashed by the blob store."""
        return self._need_blobs().get(ref)

    def _need_blobs(self) -> BlobStore:
        if self._blobs is None:
            raise BlobError("blob_store_required", "this artifact store has no blob store")
        return self._blobs

    # --- the save path -----------------------------------------------------------------

    def save(
        self,
        view: LogView,
        request: SaveRequest,
        *,
        writer: WriterId,
        run: str,
        instance: str,
        step: str,
        writes: Collection[str],
        pending_reads: Iterable[InputRef] = (),
        origin: Origin = "task",
    ) -> Saved:
        """Check ``request`` and return the facts that save it (module doc, checks 1 to 7).

        ``view`` is the owner's log as the step sees it; ``writes`` are the full kind
        names the Task may write; ``pending_reads`` are reads recorded in this same step
        and not yet committed. The returned version's ``event_seq`` is 0 until the step's
        result commits.
        """
        kind_name = _resolve_name(request.kind, writer.agent)
        kind = self._kinds.get(kind_name)
        if kind is None:
            raise SaveRefused("kind_unknown", "/kind", f"no artifact kind {kind_name!r}")
        if kind_name not in writes:
            raise SaveRefused("rights", "/kind", f"this Task does not write {kind_name!r}")
        if kind.keyed and request.key is None:
            raise SaveRefused("key_required", "/key", f"{kind_name} is keyed")
        if not kind.keyed and request.key is not None:
            raise SaveRefused("key_not_allowed", "/key", f"{kind_name} is not keyed")
        content = request.content
        hashed = _content_hash(content)
        _check_schema(kind, content)
        reads = frozenset(_lineage.recorded_reads(view, run, instance)) | frozenset(pending_reads)
        for i, primary in enumerate(request.primary_inputs):
            if primary not in reads:
                raise SaveRefused(
                    "read_not_seen", f"/primary_inputs/{i}", f"{primary} was never read here"
                )
        self._check_current(view, reads, writer)
        prior = self.latest(view, kind_name, request.key)
        number = 1 if prior is None else prior.ref.version + 1
        ref = ArtifactRef(kind_name, request.key, number)
        checked = tuple(
            self._check_relation(view, i, rr, writer, reads, ref)
            for i, rr in enumerate(request.relations)
        )
        lineage = sorted_inputs(reads)
        if prior is not None and not _may_write(kind, prior.writer, writer):
            if prior.content_hash == hashed:
                return self._adopt(view, prior, run=run, instance=instance, step=step)
            raise SaveRefused(
                "foreign_writer",
                "",
                f"{ref.slot_id()} is written by {prior.writer.task_path!r} of agent "
                f"{prior.writer.agent!r}; this instance may only adopt an equal version",
            )
        if prior is not None and self._equal(view, prior, hashed, lineage, checked):
            return Saved(prior, replayed=True)
        return self._new(
            view, kind, ref, content, hashed, lineage, checked, request, writer,
            origin=origin, run=run, instance=instance, step=step,
        )  # fmt: skip

    def _adopt(
        self, view: LogView, prior: ArtifactVersion, *, run: str, instance: str, step: str
    ) -> Saved:
        """Another writer's equal save: no new version, the version is read instead (D4)."""
        events = _lineage.reads_to_record(
            view, (prior.ref.as_input(),), run=run, instance=instance, step=step
        )
        return Saved(prior, events=events, replayed=True)

    def _equal(
        self,
        view: LogView,
        prior: ArtifactVersion,
        hashed: str,
        lineage: tuple[InputRef, ...],
        checked: tuple[_Resolved, ...],
    ) -> bool:
        """Whether a save repeats ``prior`` in content, reads and relations (D8)."""
        if prior.content_hash != hashed or prior.reads_hash != reads_hash(lineage):
            return False
        own = _relations.relations(view, carrier=prior.ref, current_only=False, blobs=self._blobs)
        mine = prior.ref.as_input()
        then = sorted(_relation_key(r.kind, r.label, r.src, r.dst, r.evidence, mine) for r in own)
        new_self = dataclasses.replace(mine, version=f"v{prior.ref.version + 1}")
        now = sorted(_relation_key(*c, new_self) for c in checked)
        return then == now

    def _new(
        self,
        view: LogView,
        kind: ArtifactKind,
        ref: ArtifactRef,
        content: Mapping[str, Any],
        hashed: str,
        lineage: tuple[InputRef, ...],
        checked: tuple[_Resolved, ...],
        request: SaveRequest,
        writer: WriterId,
        *,
        origin: Origin,
        run: str,
        instance: str,
        step: str,
    ) -> Saved:
        canonical = canonical_hash(kind, content)
        r_hash = reads_hash(lineage)
        primary = sorted_inputs(request.primary_inputs)
        payload: dict[str, Any] = {
            "ref": str(ref),
            "kind": ref.kind,
            "key": ref.key,
            "version": ref.version,
            "content_hash": hashed,
            "canonical_hash": canonical,
            "reads": [r.as_json() for r in lineage],
            "reads_hash": r_hash,
            "primary_inputs": [r.as_json() for r in primary],
            "writer": writer.as_json(),
            "origin": origin,
        }
        envelope = {"run": run, "instance": instance, "step": step}
        events: list[NewEvent] = [NewEvent("artifact.saved", payload, **envelope)]
        for n, (rkind, label, src, dst, evidence) in enumerate(checked):
            events.append(
                _relations.relation_saved(rkind, label, src, dst, evidence, ref, n, **envelope)
            )
        row = ArtifactRow(str(ref), ref.kind, ref.key, ref.version, content, hashed, canonical)
        version = ArtifactVersion(
            owner=view.owner, ref=ref, content=content, content_hash=hashed,
            canonical_hash=canonical, reads=lineage, reads_hash=r_hash, writer=writer,
            origin=origin, run=run, step=step, event_seq=0, primary_inputs=primary,
        )  # fmt: skip
        return Saved(version, events=tuple(events), rows=(row,))

    def _check_current(self, view: LogView, reads: frozenset[InputRef], writer: WriterId) -> None:
        """Check 4: every artifact read this agent wrote is still its slot's latest."""
        for read in sorted_inputs(reads):
            if read.kind != "artifact":
                continue
            ref = ArtifactRef.from_input(read)
            seen = self.version(view, ref)
            if seen is None:
                raise SaveRefused("read_invalidated", "", f"{read} is not in the store")
            if seen.writer.agent != writer.agent:
                continue  # pinned: another agent's version (D11)
            newest = self.current(view, *ref.slot())
            if newest is None or newest.ref == ref:
                continue
            if read.path is not None and read.entry_hash is not None:
                try:
                    if entry_hash(newest.content, read.path) == read.entry_hash:
                        continue
                except KeyError:
                    pass
            raise SaveRefused(
                "read_invalidated", "", f"{read} was read, and {newest.ref} has replaced it"
            )

    def _check_relation(
        self,
        view: LogView,
        i: int,
        rr: RelationRequest,
        writer: WriterId,
        reads: frozenset[InputRef],
        carrier: ArtifactRef,
    ) -> _Resolved:
        """Check 5 for the ``i``-th requested relation; its full kind, label, ends, evidence."""
        at = f"/relations/{i}"
        if rr.kind.rsplit(".", 1)[-1] in PROVENANCE_RELATIONS:
            raise SaveRefused(
                "provenance_relation_forbidden", f"{at}/kind",
                f"{rr.kind!r} is lineage, which only the runtime records",
            )  # fmt: skip
        name = _resolve_name(rr.kind, writer.agent)
        spec = self._relations.get(name)
        if spec is None:
            raise SaveRefused("relation_undeclared", f"{at}/kind", f"no relation kind {name!r}")
        if (rr.label not in spec.labels) if spec.labels else (rr.label is not None):
            allowed = list(spec.labels) or "no label"
            raise SaveRefused(
                "relation_label_invalid", f"{at}/label", f"{rr.label!r}: {name} takes {allowed}"
            )
        namespace = name.rsplit(".", 1)[0]
        src = self._end(view, rr.src, spec.src, namespace, reads, carrier, f"{at}/src")
        dst = self._end(view, rr.dst, spec.dst, namespace, reads, carrier, f"{at}/dst")
        if not rr.evidence:
            raise SaveRefused("citation_missing", f"{at}/evidence", "a relation cites evidence")
        if spec.cites == "per_end":
            for side, end in (("src", src), ("dst", dst)):
                if end != carrier.as_input() and not any(
                    x.source.same_source(end) for x in rr.evidence
                ):
                    raise SaveRefused(
                        "citation_missing", f"{at}/evidence", f"no excerpt from the {side} end"
                    )
        for j, excerpt in enumerate(rr.evidence):
            self._check_excerpt(view, excerpt, reads, f"{at}/evidence/{j}")
        return (name, rr.label, src, dst, tuple(rr.evidence))

    def _end(
        self,
        view: LogView,
        end: End,
        allowed: tuple[str, ...],
        namespace: str,
        reads: frozenset[InputRef],
        carrier: ArtifactRef,
        at: str,
    ) -> InputRef:
        """One relation end, resolved (``self`` is the carrier) and checked."""
        kinds = {_resolve_name(a, namespace) for a in allowed}
        if end == SELF:
            if carrier.kind not in kinds:
                raise SaveRefused("relation_end_invalid", at, f"{carrier.kind} is not {kinds}")
            return carrier.as_input()
        if not isinstance(end, InputRef):
            raise SaveRefused("relation_end_invalid", at, f"an end is an input or 'self': {end!r}")
        if end.kind == "record":
            declared = f"record:{end.id.split('/', 1)[0]}"
            if declared not in kinds:
                raise SaveRefused("relation_end_invalid", at, f"{declared} is not {sorted(kinds)}")
        elif end.kind == "artifact":
            ref = ArtifactRef.from_input(end)
            if ref.kind not in kinds:
                raise SaveRefused("relation_end_invalid", at, f"{ref.kind} is not {sorted(kinds)}")
            if self.version(view, ref) is None:
                raise SaveRefused("relation_end_invalid", at, f"{ref} is not in the store")
        else:
            raise SaveRefused("relation_end_invalid", at, f"a {end.kind} input is no relation end")
        if _whole(end) not in {_whole(r) for r in reads}:
            raise SaveRefused("read_not_seen", at, f"{end} was never read here")
        return _whole(end)

    def _check_excerpt(
        self, view: LogView, excerpt: Excerpt, reads: frozenset[InputRef], at: str
    ) -> None:
        source = _whole(excerpt.source)
        if source not in {_whole(r) for r in reads}:
            raise SaveRefused("read_not_seen", f"{at}/source", f"{source} was never read here")
        if not excerpt.quote.strip():
            raise SaveRefused("citation_invalid", f"{at}/quote", "an excerpt quotes something")
        if source.kind != "artifact":
            return  # a record's quote is checked by grounding (ADR-P13)
        cited = self.version(view, ArtifactRef.from_input(source))
        try:
            found, value = resolve(cited.content, excerpt.pointer) if cited else (False, None)
        except ValueError:
            found, value = False, None
        if not found:
            raise SaveRefused(
                "citation_invalid", f"{at}/pointer", f"{source} has no {excerpt.pointer!r}"
            )
        if not any(excerpt.quote in s for s in _strings(value)):
            raise SaveRefused(
                "citation_invalid", f"{at}/quote",
                f"the quote is not in {source} at {excerpt.pointer or '/'}",
            )  # fmt: skip


def _may_write(kind: ArtifactKind, owner: WriterId, writer: WriterId) -> bool:
    """Whether ``writer`` may write a slot ``owner`` wrote first (D4, S4)."""
    if kind.writers == "chain":
        return (owner.agent, owner.task_path) == (writer.agent, writer.task_path)
    return owner == writer


def _relation_key(
    kind: str,
    label: str | None,
    src: InputRef,
    dst: InputRef,
    evidence: tuple[Excerpt, ...],
    carrier: InputRef,
) -> tuple[Any, ...]:
    """A relation as compared across two saves, with the carrier end written as ``self``."""

    def end(x: InputRef) -> tuple[str, ...]:
        return ("self",) if x == carrier else x.sort_key()

    proof = tuple(sorted((x.source.sort_key(), x.quote, x.pointer) for x in evidence))
    return (kind, label or "", end(src), end(dst), proof)


def _content_hash(content: Mapping[str, Any]) -> str:
    """Check 2's encoding half: the content hash, or ``not_canonical_content``."""
    nul = nul_path(content)
    if nul is not None:
        raise SaveRefused("not_canonical_content", "/content", f"NUL (\\u0000) at {nul}")
    try:
        return content_hash(content)
    except NotCanonical as e:
        raise SaveRefused("not_canonical_content", "/content", f"{e.path}: {e.reason}") from None


def _check_schema(kind: ArtifactKind, content: Mapping[str, Any]) -> None:
    """Check 2's schema half: a JSON Schema, or a dataclass's fields."""
    schema = kind.schema
    if isinstance(schema, Mapping):
        errors = Draft202012Validator(dict(schema)).iter_errors(content)
        error = min(errors, key=lambda e: list(map(str, e.absolute_path)), default=None)
        if error is not None:
            pointer = "/content" + _json_pointer(error.absolute_path)
            raise SaveRefused("schema", pointer, error.message)
        return
    if not dataclasses.is_dataclass(schema):
        raise SaveRefused("schema", "", f"{kind.name}: a schema is a JSON Schema or a dataclass")
    fields = {f.name: f for f in dataclasses.fields(schema)}
    for name in sorted(set(content) - set(fields)):
        raise SaveRefused("schema", f"/content/{name}", f"{kind.name} has no field {name!r}")
    for name, f in sorted(fields.items()):
        required = f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        if required and name not in content:
            raise SaveRefused("schema", f"/content/{name}", f"{kind.name} needs {name!r}")
