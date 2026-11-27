"""Change and recovery through the real agent loop (ADR-P05 D1, D7 to D9, F2; ADR-P06).

The bench's request run builds two keys' chains with fake models; a person approves C1's
bundle for sending. Then C1's source record changes. Everything after that comes from
the runtime itself: ``drive_rebuilds`` opens one rebuild run, the scheduler re-enters
only C1's invalidated writers by writer key (never ``scope`` and never C2), each
rebuilt save commits what it confirms (the approval's revocation in the very step that
confirmed the bundle invalidated), the rebuilt bundle is a pending revision with its
diff review, the writer is done (F2), and the run ends complete. Accepting the review
records a fresh approval request and never a grant.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from aksha.runtime.invalidation.commands import accept_review
from aksha.runtime.invalidation.plan import plan_of
from aksha.runtime.invalidation.reviews import open_reviews
from aksha.runtime.invalidation.view import invalidation_view
from aksha.runtime.stop_policy.hold import held_auto_starts
from aksha.runtime.tasks.tree import compile_tree
from tests.support.rebuild import AGENT, OWNER, Rig, tasks

C2_WORK = ("demo.extract/C2@v1", "demo.compare/C2@v1", "demo.bundle/C2@v1")


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    r = Rig(tmp_path / "aksha.sqlite")
    r.start()
    result = r.drive()
    assert (result.state, result.outcome) == ("ended", "complete")
    r.approve("A-0001", "demo.bundle/C1@v1")
    yield r
    r.close()


def change_c1(r: Rig) -> None:
    r.change("C1", "2")


def test_change_and_recovery_through_the_loop(rig: Rig) -> None:
    r = rig
    calls_before = dict(r.calls)
    change_c1(r)
    iv = invalidation_view(r.view())
    assert iv.refs("invalidated") == ("demo.extract/C1@v1",)
    assert "approval.revoked" not in [e.type for e in r.events()]  # dirty is not confirmed

    (result,) = r.rebuild()
    run = result.run
    assert (result.state, result.outcome) == ("ended", "complete")
    started = r.events("run.started", run)[0].payload
    assert (started["cause"], started["for_runs"]) == ("rebuild", ("R-0001",))

    # Only C1's invalidated writers were re-entered, by writer key, in dependency order.
    entered = [(e.payload["cause"], e.instance) for e in r.events("task.entered", run)]
    assert entered == [
        ("rebuild", "extract[C1]"),
        ("rebuild", "compare[C1]"),
        ("rebuild", "bundle[C1]"),
    ]
    asked = {k: n - calls_before.get(k, 0) for k, n in r.calls.items()}
    assert {k: n for k, n in asked.items() if n} == {
        "extract[C1]": 1,
        "compare[C1]": 1,
        "bundle[C1]": 1,
    }  # one model call per re-entered writer, none for anything else
    assert r.world.reads.count("C2") == 1  # read once, in the request run

    # D9: the revocation commits in the step that confirmed the bundle invalidated.
    (revoked,) = r.events("approval.revoked")
    saved_compare = next(
        e for e in r.events("artifact.saved", run) if e.payload["ref"] == "demo.compare/C1@v2"
    )
    assert revoked.payload["approval"] == "A-0001"
    assert revoked.payload["target"] == "demo.bundle/C1@v1"
    assert saved_compare.step is not None
    same_commit = [e for e in r.events() if e.step == saved_compare.step]
    assert revoked.seq in {e.seq for e in r.events() if e.seq > saved_compare.seq}
    assert revoked.seq < max(e.seq for e in same_commit)  # inside the step's transaction

    # D1: the rebuilt bundle is a pending revision with its diff review; readers see v1.
    (review,) = open_reviews(r.view()).values()
    assert (str(review.old), str(review.revision)) == ("demo.bundle/C1@v1", "demo.bundle/C1@v2")
    assert review.run == run and review.instance == "bundle[C1]"
    current = r.setup.artifacts.current(r.view(), "demo.bundle", "C1")  # type: ignore[union-attr]
    assert current is not None and str(current.ref) == "demo.bundle/C1@v1"
    # F2: the writer is done although its review waits on a person; nothing is runnable.
    assert plan_of(r.view()) == ()
    popped = [e for e in r.events("stop.decided", run) if e.payload.get("pops")]
    assert ["bundle[C1]", "done"] in [list(p) for e in popped for p in e.payload["pops"]]

    # C2 and the scope were never touched.
    iv = invalidation_view(r.view())
    for ref in (*C2_WORK, "demo.scope@v1"):
        assert iv.state(ref).freshness == "current", ref
    assert held_auto_starts(r.view()) == ()

    # The person accepts: one fresh approval request for the revision, never a grant.
    assert accept_review(r.store, OWNER, review.interrupt, review.card_hash, r.ids).accepted
    fresh = [e for e in r.events("approval.requested") if e.payload.get("replaces")]
    assert [(e.payload["targets"], e.payload["replaces"]) for e in fresh] == [
        (("demo.bundle/C1@v2",), "A-0001")
    ]
    assert [e.payload["approval"] for e in r.events("approval.granted")] == ["A-0001"]
    assert not [e for e in r.events() if e.type.startswith("outbox.")]
    assert r.rebuild() == ()  # settled: nothing else opens


def test_f2_a_finish_with_the_old_version_in_place_is_refused(rig: Rig) -> None:
    """F2: not done while the plan lists the writer's version as runnable."""
    change_c1(rig)
    rig.on_first["extract[C1]"] = lambda: "finish_only"  # finishes without saving
    (result,) = rig.rebuild()
    assert result.outcome == "complete"
    checks = [e.payload for e in rig.events("completion_check.evaluated", result.run)
              if e.instance == "extract[C1]"]  # fmt: skip
    assert [(c["ok"], c["code"]) for c in checks] == [(False, "rebuild_pending"), (True, "")]
    assert rig.setup.artifacts.latest(rig.view(), "demo.extract", "C1").ref.version == 2  # type: ignore[union-attr]


def test_a_writer_that_fails_its_rebuild_ends_the_run_blocked_naming_it(rig: Rig) -> None:
    """#95: an instance that fails while the plan still lists it is not re-entered.

    The rebuilt extraction finishes without saving every time, so F2 refuses each finish
    until the stop-veto limit fails it. The plan still lists its old version, so before
    the fix the scheduler re-entered it in the same evaluation, again and again; now that
    evaluation ends the rebuild run ``blocked``, naming the instance, after one entry.
    """
    change_c1(rig)
    rig.finish_only.add("extract[C1]")
    before = rig.calls.get("extract[C1]", 0)
    (result,) = rig.rebuild()
    run = result.run
    assert (result.state, result.outcome) == ("ended", "blocked")
    (ended,) = rig.events("run.ended", run)
    assert list(ended.payload["blockers"]) == ["extract[C1]"]
    entered = [e.instance for e in rig.events("task.entered", run)]
    assert entered == ["extract[C1]"]  # entered once, never re-entered after its failure
    vetoes = rig.setup.tree.node("extract").task.max_finish_vetoes
    assert rig.calls["extract[C1]"] - before == vetoes  # one turn per refused finish
    last = rig.events("stop.decided", run)[-1].payload
    assert (last["verdict"], last["outcome"]) == ("stop", "blocked")
    assert ["extract[C1]", "fail"] in [list(p) for p in last["pops"]]
    assert last["limit"] == "stop_veto_limit"


def test_a_writer_that_fails_on_its_reentry_is_not_entered_again_in_that_evaluation(
    rig: Rig,
) -> None:
    """#95: a re-entry the Task's limit fails at once ends the run blocked, not refused.

    With a turn limit of 0 the re-entered extraction fails in the very evaluation that
    enters it. Before the fix the scheduler picked the blocked instance again in that
    evaluation until the loop bound refused it (``evaluation_loop ... did not settle``).
    """
    change_c1(rig)
    slim = tuple(replace(t, turn_limit=0) if t.id == "extract" else t for t in tasks())
    rig.setup = replace(rig.setup, tree=compile_tree(AGENT, slim))
    (result,) = rig.rebuild()
    run = result.run
    assert (result.state, result.outcome) == ("ended", "blocked")
    assert [e.instance for e in rig.events("task.entered", run)] == ["extract[C1]"]
    (decided,) = [e.payload for e in rig.events("stop.decided", run) if e.payload.get("pops")]
    assert [list(p) for p in decided["pops"]] == [["extract[C1]", "fail"]]
    assert (decided["verdict"], decided["outcome"]) == ("stop", "blocked")
    assert decided["reason"] == "blocked: extract[C1]"


def test_rebuild_run_opens_once_per_set_of_work(rig: Rig) -> None:
    """Fix 8: the same runnable work opens one run; new work opens another."""
    change_c1(rig)
    (first,) = rig.rebuild()
    assert first.outcome == "complete"
    assert rig.rebuild() == ()  # the same work is settled; no second run
    rig.change("C2", "2")
    (second,) = rig.rebuild()
    entered = [e.instance for e in rig.events("task.entered", second.run)]
    assert entered == ["extract[C2]", "compare[C2]", "bundle[C2]"]


def test_an_open_run_regenerates_its_own_work_in_place(tmp_path: Path) -> None:
    """D7: a change while the request run is open re-enters its own done work in place."""
    r = Rig(tmp_path / "aksha.sqlite")
    try:
        r.start()
        r.on_first["compare[C1]"] = lambda: change_c1(r)  # the record moves mid-run
        result = r.drive()
        assert (result.state, result.outcome) == ("ended", "complete")
        rebuilt = [e.instance for e in r.events("task.entered")
                   if e.payload["cause"] == "rebuild"]  # fmt: skip
        assert rebuilt[0] == "extract[C1]" and "extract[C2]" not in rebuilt
        assert all(e.run == "R-0001" for e in r.events("task.entered"))
        assert [e.run for e in r.events("run.started")] == ["R-0001"]  # no rebuild run
        iv = invalidation_view(r.view())
        assert iv.refs("invalidated") == () and iv.refs("dirty") == ()
        latest = r.setup.artifacts.latest(r.view(), "demo.bundle", "C1")  # type: ignore[union-attr]
        assert latest is not None and latest.content["from"] == "2"
        assert r.rebuild() == ()
    finally:
        r.close()


def test_headless_rebuild_needs_a_sandbox(tmp_path: Path) -> None:
    """Fix 8: a rebuild of a headless run is headless, and refused without a sandbox."""
    from dataclasses import replace

    from aksha.runtime.sandbox.types import Availability

    r = Rig(tmp_path / "aksha.sqlite")
    try:
        r.start(headless=True)
        r.setup = replace(r.setup, sandbox=Availability("process", "os-jail", True))
        r.drive()
        change_c1(r)
        r.setup = replace(r.setup, sandbox=None)
        assert r.rebuild() == ()  # headless, and no sandbox backend is available
        r.setup = replace(r.setup, sandbox=Availability("process", "os-jail", True))
        (result,) = r.rebuild()
        assert r.events("run.started", result.run)[0].payload["headless"] is True
    finally:
        r.close()
