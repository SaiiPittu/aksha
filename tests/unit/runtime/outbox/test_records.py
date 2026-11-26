"""External record state (ADR-P29): one record per outside record an effect created or revised.

The record is a projection over the outbox facts, never stored; its freshness is joined
from the invalidation view (ADR-P05) at read time. The bench saves a synthetic form for
candidate C1, writes it to a mock forms ledger through the outbox, moves the record it
read, and writes the rebuilt version.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.execution.types import StepPolicy
from aksha.runtime.outbox import facts
from aksha.runtime.outbox.errors import KeyFieldMissing
from aksha.runtime.outbox.outbox import enqueue
from aksha.runtime.outbox.ports import SystemOfRecord
from aksha.runtime.outbox.records import EXTERNAL_RECORDS, external_records, record_key
from aksha.runtime.outbox.types import EffectSpec
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.projections.project import apply_events, rebuild
from tests.support.invalidation import OWNER, Bench, FakeSource, record
from tests.support.systems import Ledger, NoLedger

FORM = EffectSpec(
    system="forms",
    key_fields=("/form", "/template", "/candidate"),
    semantics="reconcile",
    record_fields=("/template", "/candidate"),
)
SEND = EffectSpec(system="forms", key_fields=("/form",), semantics="reconcile")
HOOK = EffectSpec(
    system="hook", key_fields=("/form",), semantics="at_most_once", record_fields=("/candidate",)
)


def args(form: str, candidate: str = "SYN-C1", template: str = "disclosure") -> dict[str, Any]:
    return {"form": form, "candidate": candidate, "template": template}


class Forms:
    """A bench, a mock forms ledger and the worker that writes to it."""

    def __init__(self, tmp: Path) -> None:
        self.b = Bench(tmp)
        self.ledger = Ledger("forms")
        self.hook = NoLedger("hook")
        self.b.start("R-0001")

    def worker(self) -> OutboxWorker:
        systems: dict[str, SystemOfRecord] = {"forms": self.ledger, "hook": self.hook}
        policies = {"write_form": StepPolicy(max_retries=0)}
        return OutboxWorker(self.b.store, OWNER, self.b.clock, systems, policies=policies)

    def enqueue(self, a: dict[str, Any], spec: EffectSpec = FORM, tool: str = "write_form") -> str:
        out = enqueue(self.b.view(), tool, a, spec)
        assert out.event is not None
        self.b.store.append(OWNER, facts.ENQUEUE, (out.event,))
        return out.entry.key

    def write(self, a: dict[str, Any], spec: EffectSpec = FORM, tool: str = "write_form") -> str:
        key = self.enqueue(a, spec, tool)
        self.worker().drain()
        return key

    def move(self, *records: str) -> None:
        from aksha.runtime.invalidation.commands import apply_source_change

        source = FakeSource()
        source.versions[OWNER] = len([e for e in self.b.events() if e.type == "source.changed"])
        source.change(OWNER, {f"refs/{r}": "2" for r in records})
        assert apply_source_change(self.b.store, OWNER, [source], "cs").accepted


@pytest.fixture
def f(tmp_path: Path) -> Iterator[Forms]:
    forms = Forms(tmp_path)
    yield forms
    forms.b.close()


def test_enqueue_records_the_record_identity_and_the_sources_as_facts(f: Forms) -> None:
    f.b.save("R-0001", "p.form/C1", {"v": 1}, reads=[record("K")])
    key = f.enqueue(args("p.form/C1@v1"))
    (event,) = [e for e in f.b.events() if e.type == "outbox.enqueued"]
    assert event.payload["record"] == {
        "key": record_key("write_form", OWNER, args("p.form/C1@v1"), FORM),
        "fields": {"/template": "disclosure", "/candidate": "SYN-C1"},
    }
    assert tuple(event.payload["sources"]) == ("p.form/C1@v1",)
    assert event.payload["key"] == key


def test_an_effect_without_record_fields_writes_no_record_facts(f: Forms) -> None:
    f.enqueue({"form": "p.form/C1@v1"}, SEND, tool="send")
    (event,) = [e for e in f.b.events() if e.type == "outbox.enqueued"]
    assert "record" not in event.payload
    assert "sources" not in event.payload
    f.worker().drain()
    assert external_records(f.b.view()) == ()


def test_record_identity_ignores_the_request_identity() -> None:
    first = record_key("write_form", OWNER, args("p.form/C1@v1"), FORM)
    assert first == record_key("write_form", OWNER, args("p.form/C1@v2"), FORM)
    assert first != record_key("write_form", OWNER, args("p.form/C1@v1", "SYN-C2"), FORM)
    assert first != record_key("write_form", "o2", args("p.form/C1@v1"), FORM)
    assert first != record_key("fill_form", OWNER, args("p.form/C1@v1"), FORM)


def test_a_missing_record_field_is_refused_before_anything_is_written(f: Forms) -> None:
    # The loader refuses record fields outside the key fields (``record_field_not_keyed``);
    # a spec built by hand still never enqueues a request that names no record.
    loose = EffectSpec("forms", ("/form",), "reconcile", record_fields=("/template",))
    with pytest.raises(KeyFieldMissing) as e:
        enqueue(f.b.view(), "write_form", {"form": "p.form/C1@v1"}, loose)
    assert e.value.code == "record_field_missing"


def test_two_writes_of_one_record_are_two_revisions_oldest_first(f: Forms) -> None:
    f.b.save("R-0001", "p.form/C1", {"v": 1}, reads=[record("K")])
    first = f.write(args("p.form/C1@v1"))
    f.b.start("R-0002")
    f.b.save("R-0002", "p.form/C1", {"v": 2}, reads=[record("K", "2")])
    second = f.write(args("p.form/C1@v2"))
    assert first != second  # two requests, two ledger rows
    (rec,) = external_records(f.b.view())
    assert rec.system == "forms"
    assert rec.tool == "write_form"
    assert rec.fields == {"/template": "disclosure", "/candidate": "SYN-C1"}
    assert [r.key for r in rec.revisions] == [first, second]
    assert [r.ledger_id for r in rec.revisions] == ["forms-ledger-1"] * 2
    assert rec.revisions[-1].body == {"message_id": "m-2"}
    assert rec.source_artifact == ArtifactRef.parse("p.form/C1@v2")
    assert rec.freshness == "current"
    assert not rec.stale


def test_the_outside_copy_of_an_invalidated_version_reads_stale_without_a_write(
    f: Forms,
) -> None:
    f.b.save("R-0001", "p.form/C1", {"v": 1}, reads=[record("K")])
    f.write(args("p.form/C1@v1"))
    f.move("K")
    before = len(f.b.events())
    (rec,) = external_records(f.b.view())
    assert rec.source_artifact == ArtifactRef.parse("p.form/C1@v1")
    assert (rec.freshness, rec.stale) == ("invalidated", True)
    assert len(f.b.events()) == before  # D3: joined, never stored
    assert len(f.ledger.rows) == 1  # D4: no automatic write marks the outside copy


def test_records_are_one_per_record_identity_and_filtered_by_system(f: Forms) -> None:
    f.b.save("R-0001", "p.form/C1", {"v": 1})
    f.b.save("R-0001", "p.form/C2", {"v": 1})
    f.write(args("p.form/C1@v1"))
    f.write(args("p.form/C2@v1", "SYN-C2"))
    f.write(args("p.form/C1@v1", template="worksheet"))
    f.write(args("p.form/C1@v1"), HOOK, tool="trigger")
    found = external_records(f.b.view())
    assert [(r.system, r.tool, dict(r.fields)) for r in found] == [
        ("forms", "write_form", {"/template": "disclosure", "/candidate": "SYN-C1"}),
        ("forms", "write_form", {"/template": "disclosure", "/candidate": "SYN-C2"}),
        ("forms", "write_form", {"/template": "worksheet", "/candidate": "SYN-C1"}),
        ("hook", "trigger", {"/candidate": "SYN-C1"}),
    ]
    assert [r.system for r in external_records(f.b.view(), "hook")] == ["hook"]
    assert external_records(f.b.view(), "mail") == ()


def test_a_pending_revision_is_listed_and_a_refused_one_is_not_a_revision(f: Forms) -> None:
    f.b.save("R-0001", "p.form/C1", {"v": 1})
    first = f.write(args("p.form/C1@v1"))
    f.ledger.transient = 1
    f.write(args("p.form/C1@v1", template="disclosure") | {"form": "p.form/C1@v9"})
    pending = f.enqueue(args("p.form/C1@v1") | {"note": "x", "form": "p.form/C1@v8"})
    (rec,) = external_records(f.b.view())
    assert [r.key for r in rec.revisions] == [first]  # the retries-exhausted one is not
    assert rec.pending == (pending,)
    assert rec.unknown == ()


def test_an_unknown_outcome_may_hold_a_revision_and_is_listed(f: Forms) -> None:
    f.hook.fail = True
    key = f.write(args("p.form/C1@v1"), HOOK, tool="trigger")
    (rec,) = external_records(f.b.view())
    assert rec.revisions == ()
    assert rec.unknown == (key,)
    assert rec.source_artifact is None
    assert rec.freshness is None


def test_a_reconciled_completion_is_a_revision(f: Forms) -> None:
    from tests.support.steps import Crash

    f.b.save("R-0001", "p.form/C1", {"v": 1})
    f.ledger.crash_after_apply = True
    key = f.enqueue(args("p.form/C1@v1"))
    with pytest.raises(Crash):
        f.worker().drain()
    f.ledger.crash_after_apply = False
    f.worker().reconcile("landed")
    (rec,) = external_records(f.b.view())
    assert [r.key for r in rec.revisions] == [key]
    assert rec.freshness == "current"


def test_a_source_that_names_no_saved_version_has_no_freshness(f: Forms) -> None:
    f.write(args("p.form/C9@v1", "SYN-C9"))
    (rec,) = external_records(f.b.view())
    assert rec.source_artifact == ArtifactRef.parse("p.form/C9@v1")
    assert rec.freshness is None
    assert not rec.stale


def test_the_projection_rebuilt_upto_a_seq_equals_the_log_as_it_stood(f: Forms) -> None:
    f.b.save("R-0001", "p.form/C1", {"v": 1})
    f.write(args("p.form/C1@v1"))
    at = f.b.view().seq_read
    f.b.start("R-0002")
    f.b.save("R-0002", "p.form/C1", {"v": 2})
    f.write(args("p.form/C1@v2"))
    view = f.b.view()
    (old,) = external_records(view.upto(at))
    assert len(old.revisions) == 1
    # ADR-P01 D3: continuing from the state at ``at`` equals a full rebuild.
    later = (e for e in view.events if e.seq > at)
    resumed = apply_events(EXTERNAL_RECORDS, rebuild(EXTERNAL_RECORDS, view.upto(at)), later,
                           view.artifacts)  # fmt: skip
    assert resumed == rebuild(EXTERNAL_RECORDS, view)
    assert resumed != rebuild(EXTERNAL_RECORDS, view.upto(at))


BRIEFED = EffectSpec(
    system="forms",
    key_fields=("/form", "/brief", "/candidate"),
    semantics="reconcile",
    record_fields=("/candidate",),
)


def test_a_record_with_several_sources_reads_the_stalest(f: Forms) -> None:
    # ADR-P29 B2: every artifact version among the key values is a source.
    f.b.save("R-0001", "p.form/C1", {"v": 1}, reads=[record("K")])
    f.b.save("R-0001", "p.note/C1", {"v": 1}, reads=[record("J")])
    a = {"form": "p.form/C1@v1", "brief": {"from": ["p.note/C1@v1"]}, "candidate": "SYN-C1"}
    f.write(a, BRIEFED)
    (rec,) = external_records(f.b.view())
    assert rec.sources == (ArtifactRef.parse("p.form/C1@v1"), ArtifactRef.parse("p.note/C1@v1"))
    assert rec.source_artifact == ArtifactRef.parse("p.form/C1@v1")
    assert rec.freshness == "current"
    f.move("J")  # only the second source is invalidated
    (rec,) = external_records(f.b.view())
    assert (rec.freshness, rec.stale) == ("invalidated", True)
