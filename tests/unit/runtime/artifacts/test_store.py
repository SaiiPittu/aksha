"""The save path (ADR-P04 D4 to D8): its checks in order, idempotency, lineage and relations."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.artifacts.errors import SaveRefused
from aksha.runtime.artifacts.hashing import entry_hash
from aksha.runtime.artifacts.lineage import recorded_reads
from aksha.runtime.artifacts.types import (
    SELF,
    ArtifactRef,
    Excerpt,
    InputRef,
    RelationRequest,
    WriterId,
)
from tests.support.artifacts import COMPARE, GATHER, OTHER, OWNER, RUN, World, page


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    yield w
    w.close()


def _refused(code: str, fn: object, *args: object, **kwargs: object) -> SaveRefused:
    with pytest.raises(SaveRefused) as e:
        fn(*args, **kwargs)  # type: ignore[operator]
    assert e.value.code == code
    return e.value


def test_first_save_is_v1_with_its_reads_and_three_hashes(world: World) -> None:
    world.read("gather", page(1))
    saved = world.save("line", "n1", {"text": "a"})
    v = saved.version
    assert str(v.ref) == "notes.line/n1@v1"
    assert v.reads == (page(1),)
    assert all(h.startswith("sha256:") for h in (v.content_hash, v.canonical_hash, v.reads_hash))
    stored = world.artifacts.version(world.view(), v.ref)
    assert stored is not None and stored.reads == (page(1),) and stored.writer == GATHER
    assert stored.event_seq > 0 and dict(stored.content) == {"text": "a"}


def test_lineage_equals_recorded_reads(world: World) -> None:
    """Every saved version's lineage is exactly what its instance recorded, never a subset."""
    world.read("gather", page(1), page(2))
    world.save("line", "n1", {"text": "a"}, primary_inputs=(page(1),))
    world.read("compare", InputRef("artifact", "notes.line/n1", "v1"), page(3))
    world.save("summary", None, {"s": "x"}, writer=COMPARE, instance="compare")
    view = world.view()
    for instance, kind, key in (("gather", "notes.line", "n1"), ("compare", "notes.summary", None)):
        v = world.artifacts.latest(view, kind, key)
        assert v is not None
        assert frozenset(v.reads) == recorded_reads(view, RUN, instance)
    first = world.artifacts.latest(view, "notes.line", "n1")
    assert first is not None and first.primary_inputs == (page(1),)
    assert first.reads == (page(1), page(2))  # the label changed no lineage edge


def test_read_not_seen_refused(world: World) -> None:
    world.read("gather", page(1))
    e = _refused("read_not_seen", world.save, "line", "n1", {"text": "a"},
                 primary_inputs=(page(9),))  # fmt: skip
    assert e.path == "/primary_inputs/0"


def test_retry_returns_existing(world: World) -> None:
    """An equal save against the slot's latest version returns it and commits nothing."""
    world.read("gather", page(1))
    first = world.save("line", "n1", {"text": "a"})
    before = world.store.last_seq(OWNER)
    again = world.save("line", "n1", {"text": "a"})
    assert again.replayed and again.version.ref == first.version.ref and again.events == ()
    assert world.store.last_seq(OWNER) == before
    assert len(world.store.read_artifacts(OWNER)) == 1


def test_reverted_content_is_new_version(world: World) -> None:
    """v1, v2, then v1's content again saves v3, equal to v1 and current (D8)."""
    world.save("line", "n1", {"text": "a"})
    world.save("line", "n1", {"text": "b"})
    v3 = world.save("line", "n1", {"text": "a"}).version
    view = world.view()
    v1 = world.artifacts.version(view, ArtifactRef("notes.line", "n1", 1))
    assert v1 is not None and v3.ref.version == 3 and v3.content_hash == v1.content_hash
    current = world.artifacts.current(view, "notes.line", "n1")
    assert current is not None and current.ref == v3.ref
    assert [v.ref.version for v in world.artifacts.versions(view, "notes.line", "n1")] == [1, 2, 3]


def test_equal_content_with_new_reads_is_a_new_version(world: World) -> None:
    world.save("line", "n1", {"text": "a"})
    world.read("gather", page(1))
    assert world.save("line", "n1", {"text": "a"}).version.ref.version == 2


def test_c2_change_leaves_packet_c1_current(world: World) -> None:
    """Reads are versions, not kinds: a new version of one slot leaves readers of another."""
    world.save("line", "c1", {"text": "one"})
    world.save("line", "c2", {"text": "two"})
    world.read("compare", InputRef("artifact", "notes.line/c1", "v1"))
    world.save("line", "c2", {"text": "two, changed"})
    saved = world.save("summary", None, {"s": "c1 only"}, writer=COMPARE, instance="compare")
    assert saved.version.ref.version == 1


def test_a_replaced_read_is_refused(world: World) -> None:
    world.save("line", "n1", {"text": "a"})
    world.read("compare", InputRef("artifact", "notes.line/n1", "v1"))
    world.save("line", "n1", {"text": "b"})
    e = _refused("read_invalidated", world.save, "summary", None, {"s": "x"},
                 writer=COMPARE, instance="compare")  # fmt: skip
    assert "notes.line/n1@v2" in e.message


def test_a_path_scoped_read_stays_current_when_its_entry_is_unchanged(world: World) -> None:
    """S3: a new version whose entry at the read's path hashes the same is still current."""
    world.save("line", "n1", {"text": "a", "rows": [{"id": "r1"}]})
    pinned = entry_hash({"text": "a", "rows": [{"id": "r1"}]}, "/rows/0")
    read = InputRef("artifact", "notes.line/n1", "v1", path="/rows/0", entry_hash=pinned)
    world.read("compare", read)
    world.save("line", "n1", {"text": "edited", "rows": [{"id": "r1"}]})
    world.save("summary", None, {"s": "x"}, writer=COMPARE, instance="compare")
    world.save("line", "n1", {"text": "edited", "rows": [{"id": "r2"}]})
    _refused("read_invalidated", world.save, "summary", None, {"s": "y"},
             writer=COMPARE, instance="compare")  # fmt: skip


def test_another_agents_version_is_pinned(world: World) -> None:
    """D11: a read of another agent's version stays current when that agent saves again."""
    world.save("other.fact", "f1", {"v": 1}, writer=OTHER, instance="find")
    world.read("compare", InputRef("artifact", "other.fact/f1", "v1"))
    world.save("other.fact", "f1", {"v": 2}, writer=OTHER, instance="find")
    saved = world.save("summary", None, {"s": "x"}, writer=COMPARE, instance="compare")
    assert InputRef("artifact", "other.fact/f1", "v1") in saved.version.reads


def test_refusals_in_order_kind_rights_key_schema(world: World) -> None:
    _refused("kind_unknown", world.save, "nothing", None, {})
    _refused("rights", world.save, "line", "n1", {"text": "a"}, writes=("notes.summary",))
    _refused("key_required", world.save, "line", None, {"text": "a"})
    _refused("key_not_allowed", world.save, "summary", "k", {})
    e = _refused("schema", world.save, "line", "n1", {"text": 3})
    assert e.path == "/content/text"
    _refused("not_canonical_content", world.save, "line", "n1", {"text": "a", "rows": [1.5]})


def test_a_dataclass_schema_checks_fields(tmp_path: Path) -> None:
    from dataclasses import dataclass

    from aksha.runtime.artifacts.store import ArtifactStore
    from aksha.runtime.artifacts.types import ArtifactKind, SaveRequest

    @dataclass
    class Row:
        name: str
        note: str = ""

    world = World(tmp_path)
    try:
        store = ArtifactStore({"notes.row": ArtifactKind("row", Row)})
        args: dict[str, Any] = {
            "writer": GATHER,
            "run": RUN,
            "instance": "gather",
            "step": "s",
            "writes": {"notes.row"},
        }
        view = world.view()
        assert store.save(view, SaveRequest("row", None, {"name": "x"}), **args).version
        _refused("schema", store.save, view, SaveRequest("row", None, {"note": "x"}), **args)
        _refused(
            "schema", store.save, view, SaveRequest("row", None, {"name": "x", "z": 1}), **args
        )
    finally:
        world.close()


def test_one_writer_per_slot(world: World) -> None:
    """D4: another instance is refused unless it adopts an equal version as a read."""
    world.save("line", "n1", {"text": "a"})
    elsewhere = WriterId("notes", "gather", "k2")
    _refused("foreign_writer", world.save, "line", "n1", {"text": "b"},
             writer=elsewhere, instance="gather[k2]")  # fmt: skip
    adopted = world.save("line", "n1", {"text": "a"}, writer=elsewhere, instance="gather[k2]")
    assert adopted.replayed and adopted.version.ref.version == 1
    assert InputRef("artifact", "notes.line/n1", "v1") in recorded_reads(
        world.view(), RUN, "gather[k2]"
    )


def test_a_chained_kind_takes_every_instance_of_one_task(world: World) -> None:
    """S4: ``writers="chain"`` lets instances of one declared Task write in turn."""
    world.save(
        "tree", None, {"files": 1}, writer=WriterId("notes", "fix", "S1"), instance="fix[S1]"
    )
    v2 = world.save("tree", None, {"files": 2}, writer=WriterId("notes", "fix", "S2"),
                    instance="fix[S2]").version  # fmt: skip
    assert v2.ref.version == 2 and v2.writer.key == "S2"
    _refused("foreign_writer", world.save, "tree", None, {"files": 3},
             writer=WriterId("notes", "other_task", None), instance="other_task")  # fmt: skip


def _same_as(*evidence: Excerpt) -> RelationRequest:
    return RelationRequest("same_as", page(1), page(2), evidence=evidence)


def test_relation_needs_citation(world: World) -> None:
    world.read("gather", page(1), page(2))
    e = _refused("citation_missing", world.save, "merge", "m1", {}, relations=(_same_as(),))
    assert e.path == "/relations/0/evidence"
    ok = _same_as(world.cite("gather", page(1), "Acme Sensors GmbH"))
    saved = world.save("merge", "m1", {}, relations=(ok,))
    rels = world.artifacts.relations(world.view(), kind="notes.same_as")
    assert [(r.src, r.dst, r.carrier) for r in rels] == [(page(1), page(2), saved.version.ref)]


def test_provenance_edge_forbidden(world: World) -> None:
    world.read("gather", page(1), page(2))
    for name in ("derived_from", "supersedes", "notes.derived_from"):
        bad = RelationRequest(name, page(1), page(2), evidence=(Excerpt(page(1), "q"),))
        _refused("provenance_relation_forbidden", world.save, "merge", "m1", {}, relations=(bad,))


def test_relation_shape_refusals(world: World) -> None:
    world.save("line", "n1", {"text": "costs 279 a month"})
    world.save("line", "n2", {"text": "costs 300 a month"})
    n1 = InputRef("artifact", "notes.line/n1", "v1")
    n2 = InputRef("artifact", "notes.line/n2", "v1")
    world.read("compare", n1, n2, page(1))
    cite = (Excerpt(n1, "279", "/text"),)

    def save(rel: RelationRequest) -> object:
        return world.save("summary", None, {}, writer=COMPARE, instance="compare", relations=(rel,))

    _refused("relation_undeclared", save, RelationRequest("unknown", n1, n2, cite))
    _refused("relation_label_invalid", save, RelationRequest("contradicts", n1, n2, cite))
    _refused("relation_label_invalid", save, RelationRequest("contradicts", n1, n2, cite, "size"))
    _refused(
        "relation_end_invalid", save, RelationRequest("contradicts", page(1), n2, cite, "price")
    )
    gone = InputRef("artifact", "notes.line/n9", "v1")
    _refused("relation_end_invalid", save, RelationRequest("contradicts", gone, n2, cite, "price"))
    world.save("line", "n3", {"text": "x"})
    n3 = InputRef("artifact", "notes.line/n3", "v1")
    _refused("read_not_seen", save, RelationRequest("contradicts", n3, n2, cite, "price"))
    wrong = (Excerpt(n1, "not there", "/text"),)
    _refused("citation_invalid", save, RelationRequest("contradicts", n1, n2, wrong, "price"))
    nowhere = (Excerpt(n1, "279", "/missing"),)
    _refused("citation_invalid", save, RelationRequest("contradicts", n1, n2, nowhere, "price"))
    unread = (Excerpt(page(7), "q"),)
    _refused("read_not_seen", save, RelationRequest("contradicts", n1, n2, unread, "price"))


def test_conflicts_are_relations(world: World) -> None:
    """A conflict is a ``contradicts`` relation with its evidence, not a field."""
    world.save("line", "n1", {"text": "costs 279 a month"})
    world.save("line", "n2", {"text": "costs 300 a month"})
    n1 = InputRef("artifact", "notes.line/n1", "v1")
    n2 = InputRef("artifact", "notes.line/n2", "v1")
    world.read("compare", n1, n2)
    rel = RelationRequest("contradicts", n1, n2, (Excerpt(n1, "279", "/text"),), "price")
    world.save("summary", None, {"s": "two prices"}, writer=COMPARE, instance="compare",
               relations=(rel,))  # fmt: skip
    (found,) = world.artifacts.relations(world.view(), touching=ArtifactRef("notes.line", "n2", 1))
    assert (found.kind, found.label, found.evidence[0].quote) == (
        "notes.contradicts",
        "price",
        "279",
    )


def test_per_end_needs_evidence_from_each_end_and_self_is_the_carrier(world: World) -> None:
    world.save("line", "n1", {"text": "the long line"})
    n1 = InputRef("artifact", "notes.line/n1", "v1")
    world.read("compare", n1)
    rel = RelationRequest("summarises", SELF, n1, (Excerpt(n1, "long"),))
    saved = world.save("summary", None, {"s": "short"}, writer=COMPARE, instance="compare",
                       relations=(rel,))  # fmt: skip
    (found,) = world.artifacts.relations(world.view(), kind="notes.summarises")
    assert found.src == saved.version.ref.as_input()
    world.save("line", "n2", {"text": "other"})
    n2 = InputRef("artifact", "notes.line/n2", "v1")
    world.read("compare", n2)
    bad = RelationRequest("summarises", SELF, n2, (Excerpt(n1, "long"),))
    _refused("citation_missing", world.save, "summary", None, {"s": "x"}, writer=COMPARE,
             instance="compare", relations=(bad,))  # fmt: skip


def test_a_new_carrier_version_retracts_its_old_relations(world: World) -> None:
    world.read("gather", page(1), page(2))
    world.save("merge", "m1", {}, relations=(_same_as(world.cite("gather", page(1), "Acme")),))
    world.save("merge", "m1", {"undone": True})
    view = world.view()
    assert world.artifacts.relations(view, kind="notes.same_as") == ()
    assert world.artifacts.components(view, "notes.same_as") == ()


def test_equal_content_with_other_relations_is_a_new_version(world: World) -> None:
    world.read("gather", page(1), page(2))
    acme = world.cite("gather", page(1), "Acme")
    world.save("merge", "m1", {}, relations=(_same_as(acme),))
    same = world.save("merge", "m1", {}, relations=(_same_as(acme),))
    assert same.replayed
    gmbh = world.cite("gather", page(2), "Acme GmbH")
    other = world.save("merge", "m1", {}, relations=(_same_as(gmbh),))
    assert other.version.ref.version == 2


def test_the_blob_helpers_need_a_blob_store(world: World) -> None:
    ref = world.artifacts.put_blob(b"bytes")
    assert world.artifacts.get_blob(ref) == b"bytes"
    from aksha.runtime.artifacts.errors import BlobError
    from aksha.runtime.artifacts.store import ArtifactStore

    with pytest.raises(BlobError) as e:
        ArtifactStore({}).put_blob(b"x")
    assert e.value.code == "blob_store_required"


def test_frozen_content_is_checked_against_its_schema(world: World) -> None:
    """A tool body's arguments are frozen; the schema check reads them as plain JSON."""
    from aksha.runtime.canonical import freeze

    saved = world.save("line", "n1", freeze({"text": "a", "rows": [1, 2]}))
    assert str(saved.version.ref) == "notes.line/n1@v1"
    _refused("schema", world.save, "line", "n2", freeze({"rows": []}))


# --- supersedes (ADR-P09 D4, D11): a save that replaces its slot's latest version ------------


def test_supersedes_commits_a_typed_relation_in_the_save(world: World) -> None:
    world.save("line", "n1", {"text": "first"})
    saved = world.save("line", "n1", {"text": "second"}, supersedes="notes.line/n1@v1")
    rel = [e for e in saved.events if e.type == "relation.saved"]
    assert len(rel) == 1
    p = rel[0].payload
    assert p["kind"] == "supersedes"
    assert p["src"] == {"kind": "artifact", "id": "notes.line/n1", "version": "v2"}
    assert p["dst"] == {"kind": "artifact", "id": "notes.line/n1", "version": "v1"}
    assert p["evidence"] == [] and p["carrier"] == "notes.line/n1@v2"
    (found,) = world.artifacts.relations(world.view(), kind="supersedes")
    assert str(found.carrier) == "notes.line/n1@v2"


def test_supersedes_names_its_slots_latest_version(world: World) -> None:
    world.save("line", "n1", {"text": "first"})
    world.save("line", "n1", {"text": "second"})
    world.save("line", "n2", {"text": "other"})
    for target in ("notes.line/n1@v1", "notes.line/n2@v1", "notes.line/n1@v9", "nonsense"):
        _refused("supersedes_invalid", world.save, "line", "n1", {"text": "x"},
                 supersedes=target)  # fmt: skip
    _refused("supersedes_invalid", world.save, "line", "n3", {"text": "x"},
             supersedes="notes.line/n3@v1")  # fmt: skip


def test_a_read_its_writer_superseded_in_this_run_stays_current(world: World) -> None:
    """M5: an instance that read v1 and superseded it may still save what read v1."""
    world.save("line", "n1", {"text": "first"})
    world.read("gather", InputRef("artifact", "notes.line/n1", "v1"))
    world.save("line", "n1", {"text": "second"}, supersedes="notes.line/n1@v1")
    saved = world.save("line", "n2", {"text": "uses n1"})
    assert InputRef("artifact", "notes.line/n1", "v1") in saved.version.reads


def test_a_plain_revision_in_the_same_run_still_refuses_the_older_read(world: World) -> None:
    """M5 covers supersession only: a revision without ``supersedes`` leaves check 4 as it was."""
    world.save("line", "n1", {"text": "first"})
    world.read("gather", InputRef("artifact", "notes.line/n1", "v1"))
    world.save("line", "n1", {"text": "second"})
    e = _refused("read_invalidated", world.save, "line", "n2", {"text": "uses n1"})
    assert "notes.line/n1@v2" in e.message
