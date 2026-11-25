"""The change-and-recovery demo (ADR-P05 worked example) end to end on SQLite.

A contributor record and a prior-art query of candidate C1 change after the run that wrote
the work ended. The tests walk the answer key step by step: what is invalidated, what is
dirty, what the plan rebuilds, the two diff reviews in dependency order, one revocation,
one fresh approval request and zero sends, with C2's chain and the unrelated research left
alone.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import WriterId
from aksha.runtime.execution.recovery import recover_owner
from aksha.runtime.invalidation.commands import (
    accept_review,
    apply_source_change,
    check_sources,
)
from aksha.runtime.invalidation.plan import rebuild_plan, runnable, writer_has_rebuild
from aksha.runtime.invalidation.rebuild import open_rebuild_runs, rebuild_brief
from aksha.runtime.invalidation.reviews import open_reviews
from aksha.runtime.projections.view import load_view
from aksha.runtime.storage.ports import EventStore
from tests.support.invalidation import (
    OWNER,
    SELECT,
    Bench,
    FakeQueries,
    FakeSource,
    build_demo,
    query,
    record,
    ref,
)
from tests.support.steps import Crash, FaultStore

C2_CHAIN = ("p.extract/C2@v1", "p.prior/C2@v1", "p.compare/C2@v1", "p.recommend/C2@v1")
RESEARCH = ("r.finding/F1@v1", "r.matrix@v1")


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[Bench]:
    b = Bench(tmp_path)
    build_demo(b)
    yield b
    b.close()


def change_c1(b: Bench, source: FakeSource, queries: FakeQueries) -> None:
    source.change(OWNER, {"refs/K-C1": "2"})
    queries.hashes[(OWNER, "Q-C1")] = "h2"
    receipt = apply_source_change(b.store, OWNER, [source], "c1-change", run_query=queries)
    assert receipt.accepted


def untouched(b: Bench) -> None:
    iv = b.iv()
    for r in (*C2_CHAIN, *RESEARCH):
        assert iv.state(r).freshness == "current", r


def test_s6_change_and_recovery(bench: Bench) -> None:
    b = bench
    source, queries = FakeSource(), FakeQueries()
    change_c1(b, source, queries)
    iv = b.iv()
    assert iv.refs("invalidated") == ("p.extract/C1@v1", "p.prior/C1@v1")
    assert set(iv.refs("dirty")) == {
        "p.compare/C1@v1", "p.recommend/C1@v1", "p.table@v1", "p.selection@v1",
        "p.form/C1@v1", "p.form/C2@v1", "p.packet/C1@v1", "p.packet/C2@v1",
    }  # fmt: skip
    assert iv.state("p.packet/C1@v1").via[-1] in ("record:refs/K-C1@1", "query:Q-C1@h1")
    assert "approval.revoked" not in b.types()  # dirty is not confirmed: nothing revoked
    untouched(b)
    plan = rebuild_plan(iv)
    assert [(str(e.invalidated), e.mode) for e in plan] == [
        ("p.extract/C1@v1", "regenerate"),
        ("p.prior/C1@v1", "regenerate"),
    ]

    (run,) = open_rebuild_runs(b.store, OWNER, b.ids)
    started = b.events()[-1]
    assert started.payload["for_runs"] == ("R-0001",)
    assert started.payload["rebuild_key"] == f"rebuild:{OWNER}/p/1"
    brief = rebuild_brief(b.view(), iv, plan[0])
    assert brief.current_reads == (record("K-C1", "2"),)

    # Step 1: the extraction and the prior-art result rebuild with different content.
    b.save(run, "p.extract/C1", {"contributors": ["a", "b"]}, reads=[record("K-C1", "2")])
    b.save(run, "p.prior/C1", {"families": ["A", "E"]}, reads=[query("Q-C1", "h2")])
    iv = b.iv()
    assert iv.refs("invalidated") == ("p.compare/C1@v1", "p.form/C1@v1")
    plan = rebuild_plan(iv)
    assert [str(e.invalidated) for e in runnable(plan)] == ["p.compare/C1@v1"]
    (held,) = [e for e in plan if not e.runnable]
    assert held.invalidated == ref("p.form/C1@v1") and held.held_by == ("artifact:p.selection@v1",)

    # Step 2: comparison, recommendation and table rebuild with different content.
    rows = {"rows": [{"id": "F1", "state": "disclosed"}]}
    b.save(run, "p.compare/C1", rows, reads=[ref("p.prior/C1@v2").as_input()])
    b.save(run, "p.recommend/C1", {"go": True, "closest": "E"},
           reads=[ref("p.compare/C1@v2").as_input()])  # fmt: skip
    both = [ref("p.recommend/C1@v2").as_input(), ref("p.recommend/C2@v1").as_input()]
    b.save(run, "p.table", {"order": ["C2", "C1"]}, reads=both)
    iv = b.iv()
    assert iv.refs("invalidated") == ("p.selection@v1", "p.form/C1@v1")
    (entry,) = runnable(rebuild_plan(iv))
    assert (entry.writer, entry.mode) == (SELECT, "copy_choice")

    # Step 3: the selection copies the person's choice; a review opens even though equal.
    b.save(run, "p.selection", {"chosen": ["C1", "C2"]}, reads=[ref("p.table@v2").as_input()],
           writer=SELECT, origin="copy_choice", review="I-0001")  # fmt: skip
    current = b.current("p.selection")
    assert current is not None and current.ref == ref("p.selection@v1")  # readers see the old
    iv = b.iv()
    assert iv.state("p.selection@v1").open_revision == ref("p.selection@v2")
    assert not writer_has_rebuild(rebuild_plan(iv), SELECT)  # F2: done, waiting on a person
    assert runnable(rebuild_plan(iv)) == ()  # prepare is held behind the selection review
    assert iv.summary().waiting == ("p.selection@v1",)
    (card,) = open_reviews(b.view()).values()
    reviewed = [e for e in b.events() if e.type == "interrupt.raised"][-1]
    assert reviewed.payload["hunks"] == () and reviewed.payload["changed_reads"]

    # Step 4: the person accepts; the forms of C2 come back current by cutoff.
    assert accept_review(b.store, OWNER, "I-0001", card.card_hash, b.ids).accepted
    iv = b.iv()
    assert iv.state("p.form/C2@v1").freshness == "current_by_cutoff"
    assert iv.refs("invalidated") == ("p.form/C1@v1",)
    assert "approval.revoked" not in b.types()

    # Step 5: C1's form rebuilds different: its packet is invalidated, A-0001 revoked with it.
    b.save(run, "p.form/C1", {"names": ["a", "b"]},
           reads=[ref("p.selection@v2").as_input(), ref("p.extract/C1@v2").as_input()])  # fmt: skip
    iv = b.iv()
    (entry,) = rebuild_plan(iv)
    assert (str(entry.invalidated), entry.mode) == ("p.packet/C1@v1", "propose")
    revoked = [e for e in b.events() if e.type == "approval.revoked"]
    assert [e.payload["approval"] for e in revoked] == ["A-0001"]

    # Step 6: one diff review for the packet, then one fresh approval request, never a grant.
    b.save(run, "p.packet/C1", {"forms": ["C1"], "names": 2},
           reads=[ref("p.form/C1@v2").as_input()], review="I-0002")  # fmt: skip
    (card,) = open_reviews(b.view()).values()
    assert accept_review(b.store, OWNER, "I-0002", card.card_hash, b.ids).accepted
    requested = [
        e for e in b.events() if e.type == "approval.requested" and "replaces" in e.payload
    ]
    assert [(e.payload["targets"], e.payload["replaces"]) for e in requested] == [
        (("p.packet/C1@v2",), "A-0001")
    ]
    types = b.types()
    assert "approval.granted" not in types[types.index("source.changed") :]
    assert not [t for t in types if t.startswith("outbox.")]  # zero sends
    raised = [e.payload["old"] for e in b.events()
              if e.type == "interrupt.raised" and e.payload.get("kind") == "review"]  # fmt: skip
    assert raised == ["p.selection@v1", "p.packet/C1@v1"]  # selection card before packet card
    iv = b.iv()
    assert iv.refs("invalidated") == () and iv.refs("dirty") == ()
    untouched(b)


def test_rebuild_run_after_run_ended(bench: Bench) -> None:
    source, queries = FakeSource(), FakeQueries()
    change_c1(bench, source, queries)
    assert open_rebuild_runs(bench.store, OWNER, bench.ids) == ("R-0010",)
    assert open_rebuild_runs(bench.store, OWNER, bench.ids) == ()  # the agent has an open run


def test_open_run_regenerates_in_place(tmp_path: Path) -> None:
    b = Bench(tmp_path)
    try:
        b.start("R-0001")
        b.save("R-0001", "p.extract/C1", {"n": 1}, reads=[record("K-C1")])
        source = FakeSource()
        source.change(OWNER, {"refs/K-C1": "2"})
        apply_source_change(b.store, OWNER, [source], "c")
        assert rebuild_plan(b.iv()) and open_rebuild_runs(b.store, OWNER, b.ids) == ()
    finally:
        b.close()


def test_change_set_two_owners(tmp_path: Path) -> None:
    b = Bench(tmp_path)
    try:
        for owner in ("o1", "o2"):
            b.owner = owner
            b.start("R-0001")
            b.save("R-0001", "p.extract/C1", {"n": 1}, reads=[record("K-C1")])
            b.end("R-0001")
        before = [e.seq for e in b.events("o2")]
        source = FakeSource()
        source.change("o1", {"refs/K-C1": "2"})
        apply_source_change(b.store, "o1", [source], "c")
        apply_source_change(b.store, "o2", [source], "c")  # nothing moved for o2
        assert b.iv("o1").refs("invalidated") == ("p.extract/C1@v1",)
        assert b.iv("o2").refs("invalidated") == ()
        o2 = b.events("o2")
        assert [e.type for e in o2 if e.seq not in before] == ["command.accepted"]
    finally:
        b.close()


def test_recovery_source_check_records_what_moved(bench: Bench) -> None:
    source, queries = FakeSource(), FakeQueries()
    source.change(OWNER, {"refs/K-C1": "2"})

    def check(store: EventStore, owner: str) -> tuple[str, ...]:
        return check_sources(store, owner, [source], run_query=queries)

    report = recover_owner(bench.store, OWNER, {}, source_check=check)
    assert report.sources_moved == ("refs",)
    changed = [e for e in bench.events() if e.type == "source.changed"]
    assert [e.payload["cause"] for e in changed] == ["recovery"]
    assert {q for _, q in queries.calls} == {"Q-C1", "Q-C2"}
    assert recover_owner(bench.store, OWNER, {}, source_check=check).sources_moved == ()


def test_crash_during_invalidation(bench: Bench) -> None:
    """Crash point 8: after a kill mid-rebuild, the invalidated set equals the answer key."""
    source, queries = FakeSource(), FakeQueries()
    change_c1(bench, source, queries)
    (run,) = open_rebuild_runs(bench.store, OWNER, bench.ids)
    answer = bench.iv().refs("invalidated")
    real = bench.store
    bench.store = FaultStore(real, die_on=lambda boundary, _: boundary == "U2")  # type: ignore[assignment]
    with pytest.raises(Crash):
        bench.save(run, "p.extract/C1", {"contributors": ["b"]}, reads=[record("K-C1", "2")])
    bench.store = real
    assert bench.iv().refs("invalidated") == answer
    assert load_view(real, OWNER).seq_read == bench.events()[-1].seq


def test_no_rebuild_run_while_held(bench: Bench) -> None:
    """TC-4: a person's stop of the agent's last run holds every automatic start."""
    from aksha.runtime.cancellation.command import stop
    from aksha.runtime.cancellation.ending import honour_stop

    bench.start("R-0002")
    stop(bench.store, OWNER, "R-0002")
    honour_stop(bench.store, OWNER, "R-0002", "turn_end")
    change_c1(bench, FakeSource(), FakeQueries())
    assert rebuild_plan(bench.iv())
    assert open_rebuild_runs(bench.store, OWNER, bench.ids) == ()
    from aksha.runtime.cancellation.hold import held_auto_starts

    assert held_auto_starts(bench.view()) == ()


def test_unrelated_writer_is_not_touched(bench: Bench) -> None:
    change_c1(bench, FakeSource(), FakeQueries())
    plan = rebuild_plan(bench.iv())
    assert WriterId("r", "r.finding_F1", None) not in {e.writer for e in plan}
