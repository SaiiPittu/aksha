"""Resume on the existing session against a fresh context, through the real loop (decision 36).

The resume bench (``tests/support/resume.py``) builds two keys' chains with fake models:
``extract`` reads its source record in turn 1 and saves in turn 2; ``compare`` and
``bundle`` read their input through their context and save in one turn. Then C1's
record changes and the runtime rebuilds. Everything asserted here comes from the runtime:
the setting on ``run.started``, the choice on ``task.entered{reentry}``, and the context
each re-entered instance's model step was sent.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from aksha.runtime.context.reentry import (
    reentry_divergences,
    runs_to_load,
    scoped_view,
)
from aksha.runtime.context.types import ContextPolicy, ResumePolicy
from aksha.runtime.loop.drive import drive
from aksha.runtime.stop_policy.errors import StopRefused
from tests.support.killresume import COMPLETED, recover
from tests.support.resume import LENIENT, ResumeRig, texts

ROOT = Path(__file__).resolve().parents[2]
RESUMED_SESSION = {"run": "R-0001", "instance": "extract[C1]", "from_turn": 1, "through_turn": 2}


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Any]:
    rigs: list[ResumeRig] = []

    def build(**kwargs: Any) -> ResumeRig:
        r = ResumeRig(tmp_path / f"a{len(rigs)}.sqlite", **kwargs)
        rigs.append(r)
        return r

    yield build
    for r in rigs:
        r.close()


def settle(r: ResumeRig, setting: str | None) -> str:
    """The request run (with ``setting``), then C1's change; returns the rebuild run."""
    extra = {} if setting is None else {"reentry": setting}
    r.start(**extra)
    assert r.drive().outcome is not None
    r.change("C1", "2")
    (result,) = r.rebuild()
    return result.run


def choice(r: ResumeRig, run: str, instance: str) -> dict[str, Any]:
    (found,) = [c for i, c in r.entries(run) if i == instance]
    return found


# --- the default and the setting -------------------------------------------------------------


def test_fresh_is_the_default_and_is_recorded(make: Any) -> None:
    r = make()
    run = settle(r, None)
    assert r.events("run.started", run)[0].payload["reentry"] == "fresh"
    modes = [(i, c["mode"], c.get("reason")) for i, c in r.entries(run)]
    assert modes == [
        ("extract[C1]", "fresh", "resume_disabled"),
        ("compare[C1]", "fresh", "resume_disabled"),
        ("bundle[C1]", "fresh", "resume_disabled"),
    ]
    extract = choice(r, run, "extract[C1]")
    # The session is measured even when it is not resumed, so an evaluation can compare.
    assert (extract["session_turns"], extract["session"]) == (2, ())
    assert extract["read_runs"] == ("R-0001", run)  # the brief reads the old version
    first = r.requests_of(run, "extract[C1]")[0]
    assert texts(first)[2].startswith("Rebuild: your earlier version demo.extract/C1@v1")
    assert [m.role for m in first.messages].count("assistant") == 0  # no earlier turn
    assert r.setup.artifacts.latest(r.view(), "demo.bundle", "C1").content["from"] == "2"


def test_resume_continues_the_writers_own_session(make: Any) -> None:
    r = make()
    run = settle(r, "resume")
    assert r.events("run.started", run)[0].payload["reentry"] == "resume"  # inherited
    extract = choice(r, run, "extract[C1]")
    assert (extract["mode"], extract.get("reason")) == ("resume", None)
    assert [dict(s) for s in extract["session"]] == [RESUMED_SESSION]
    assert extract["read_runs"] == ("R-0001", run)
    assert extract["stale_chars"] > 0 and extract["session_chars"] > extract["stale_chars"]

    first = r.requests_of(run, "extract[C1]")[0]
    said = texts(first)
    # The session's turns are there, with the stale record result masked in place ...
    roles = [m.role for m in first.messages]
    assert roles.count("assistant") == 2
    stale = [m.text for m in first.messages if m.role == "tool" and "superseded" in (m.text or "")]
    assert stale == [
        "[superseded: record refs/K-C1 changed, the current version is in the change notice below]"
    ]
    assert not any('"version":"1"' in t for t in said)  # the old body never shows
    # ... then the runtime's re-entry notice, and the change notice once at the end.
    notice = next(t for t in said if t.startswith("[runtime: re-entry of extract[C1]]"))
    assert "R-0001 extract[C1] turns 1 to 2" in notice
    assert "demo.extract/C1@v1 is out of date" in notice
    assert "record refs/K-C1@1 (moved) is now 2" in notice
    assert sum("Change notice:" in t for t in said) == 1
    assert not any(t.startswith("Rebuild:") for t in said)  # no fresh brief

    # compare and bundle read their input through their context: a premise, so fresh.
    for instance in ("compare[C1]", "bundle[C1]"):
        c = choice(r, run, instance)
        assert (c["mode"], c["reason"], c["session"]) == ("fresh", "premise_changed", ()), instance
    # The outputs are the ones a fresh rebuild makes.
    assert r.setup.artifacts.latest(r.view(), "demo.bundle", "C1").content["from"] == "2"
    assert r.rebuild() == ()


def test_an_explicit_setting_overrides_the_inherited_one(make: Any) -> None:
    r = make()
    r.setup = replace(r.setup, reentry="resume")
    run = settle(r, None)  # the request run is fresh; the opener says resume
    assert r.events("run.started", run)[0].payload["reentry"] == "resume"
    assert choice(r, run, "extract[C1]")["mode"] == "resume"


def test_premise_exempt_lets_a_context_reader_resume(make: Any) -> None:
    r = make(resume=replace(LENIENT, premise_exempt=frozenset({"demo.extract"})))
    run = settle(r, "resume")
    compare = choice(r, run, "compare[C1]")
    assert compare["mode"] == "resume"
    assert [dict(s)["instance"] for s in compare["session"]] == ["compare[C1]"]
    said = texts(r.requests_of(run, "compare[C1]")[0])
    assert any("[artifact demo.extract/C1@v2, full]" in t for t in said)  # current read block
    assert choice(r, run, "bundle[C1]")["reason"] == "premise_changed"


def test_new_inputs_are_listed_in_the_notice(make: Any) -> None:
    """F5: an input that entered a read of every key since the session is named."""
    from aksha.runtime.context.reentry import new_reads
    from aksha.runtime.tasks.types import InstanceRef, Read

    r = make()
    settle(r, "resume")
    tree = r.setup.tree
    node = tree.node("compare")
    wide = replace(node.task, reads=(Read("extract", "all"),))
    patched = replace(tree, nodes={**tree.nodes, "compare": replace(node, task=wide)})
    ref = InstanceRef("R-0001", "compare", "C1")
    seen = [r.setup.artifacts.latest(r.view(), "demo.extract", "C1").ref.as_input()]
    assert new_reads(r.view(), patched, ref, seen) == ("demo.extract/C2@v1",)
    assert new_reads(r.view(), tree, ref, seen) == ()  # an own-key read lists nothing


# --- the fresh triggers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"resume": ResumePolicy()}, None),  # the defaults accept a two-turn session
        ({"resume": ResumePolicy(min_prior_turns=3)}, "session_missing"),
        ({"taint": True}, "tainted_session"),
        ({"repeat": True}, "prior_looping"),
        ({"extract_turn_limit": 2}, "prior_looping"),
        ({"boom": True}, "prior_failed"),
        ({"resume": replace(LENIENT, max_stale_permille=0)}, "mostly_stale"),
        ({"resume": replace(LENIENT, headroom_chars=10**6)}, "budget_exceeded"),
    ],
)
def test_each_trigger_falls_back_to_fresh(
    make: Any, kwargs: dict[str, Any], reason: str | None
) -> None:
    r = make(**kwargs)
    run = settle(r, "resume")
    c = choice(r, run, "extract[C1]")
    assert (c["mode"], c.get("reason")) == ("resume" if reason is None else "fresh", reason)
    said = texts(r.requests_of(run, "extract[C1]")[0])
    if reason is None:
        assert not any(t.startswith("Rebuild:") for t in said)
    else:  # a fresh context: the brief, and none of the session's turns
        assert any(t.startswith("Rebuild:") for t in said)
        assert c["session"] == ()
        assert [m.role for m in r.requests_of(run, "extract[C1]")[0].messages].count(
            "assistant"
        ) == 0


def test_definition_changed_falls_back_to_fresh(make: Any) -> None:
    r = make()
    r.start(reentry="resume")
    r.drive()
    r.change("C1", "2")
    r.setup = replace(r.setup, definition_hash="sha256:" + "e" * 64)
    (result,) = r.rebuild()
    c = choice(r, result.run, "extract[C1]")
    assert (c["mode"], c["reason"]) == ("fresh", "definition_changed")


def test_split_sessions_are_never_merged(make: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """F1: versions from two writer instances (a run serving two runs) never merge."""
    import aksha.runtime.context.reentry as reentry
    from aksha.runtime.tasks.types import InstanceRef

    r = make()
    settle(r, "resume")
    monkeypatch.setattr(
        reentry, "_origins", lambda *_: (("R-0001", "extract[C1]"), ("R-0009", "extract[C1]"))
    )
    view = r.view()
    ref = InstanceRef("R-0002", "extract", "C1")
    got = reentry.choose_reentry(view, r.setup.tree, ref, r.setup.policy())
    assert (got.mode, got.reason, got.session) == ("fresh", "sessions_split", ())


def test_session_comes_from_the_writer_not_from_for_runs(make: Any) -> None:
    """F1: a second request run that reused C1's work adds nothing to the session."""
    r = make()
    r.start("R-0001", reentry="resume")
    r.drive("R-0001")
    r.start("R-0002", reentry="resume")
    r.drive("R-0002")  # its extract[C1] is done on entry: no turn of its own
    r.change("C1", "2")
    (result,) = r.rebuild()
    c = choice(r, result.run, "extract[C1]")
    assert [dict(s) for s in c["session"]] == [RESUMED_SESSION]


# --- in place, and an instance that is running ------------------------------------------------


@pytest.mark.parametrize("setting", ["fresh", "resume"])
def test_in_place_reentry_honours_the_setting(make: Any, setting: str) -> None:
    r = make()
    r.start(reentry=setting)
    r.on_first["compare[C1]"] = lambda: r.change("C1", "2")  # the record moves mid-run
    assert r.drive().outcome == "complete"
    c = choice(r, "R-0001", "extract[C1]")
    assert c["from_turn"] == 3  # turns continue in the run
    third = r.requests_of("R-0001", "extract[C1]")[2]
    said = texts(third)
    if setting == "fresh":
        assert (c["mode"], c["reason"]) == ("fresh", "resume_disabled")
        assert any(t.startswith("Rebuild:") for t in said)
        assert [m.role for m in third.messages].count("assistant") == 0  # turns 1, 2 hidden
    else:
        assert (c["mode"], [dict(s) for s in c["session"]]) == ("resume", [RESUMED_SESSION])
        assert [m.role for m in third.messages].count("assistant") == 2
        assert any(t.startswith("[runtime: re-entry of extract[C1]]") for t in said)
    assert r.setup.artifacts.latest(r.view(), "demo.bundle", "C1").content["from"] == "2"


def test_a_fresh_in_place_reentry_never_summarises_the_turns_before_it(make: Any) -> None:
    """A summary is the entry's own: a fresh re-entry never carries its old turns in one."""
    from aksha.runtime.context.types import SummaryPolicy

    tight = ContextPolicy(
        high_water_chars=200, low_water_chars=100, context_limit_chars=50_000,
        read_block_cap_chars=10_000, keep_recent_pairs=1,
        summary=SummaryPolicy(keep_recent_pairs=1),
    )  # fmt: skip
    r = make(context=tight)
    r.start()
    r.on_first["compare[C1]"] = lambda: r.change("C1", "2")
    r.drive()
    assert choice(r, "R-0001", "extract[C1]")["mode"] == "fresh"
    summaries = [e.step for e in r.events("step.started") if str(e.step).endswith("/compact")]
    assert "R-0001/extract[C1]/t03/compact" not in summaries
    third = r.requests_of("R-0001", "extract[C1]")[2]
    assert not any(t.startswith("Summary of turns") for t in texts(third))


def test_a_running_instance_gets_marks_and_a_notice_at_the_next_turn(make: Any) -> None:
    """F7: a read that moves between two turns of one entry is masked at the boundary."""
    r = make()
    r.start()

    def finished(step: str) -> bool:
        return any(e.step == step for e in r.events("step.completed"))

    while not finished("R-0001/extract[C1]/t01/c01"):  # one evaluation at a time
        with contextlib.suppress(StopRefused):
            drive(r.setup, r.executor, "R-0001", max_evaluations=1)
    r.change("C1", "2")  # lands between turn 1 and turn 2 of extract[C1]
    with contextlib.suppress(StopRefused):  # the scripted model never reads again
        r.drive()
    first, second = r.requests_of("R-0001", "extract[C1]")[:2]
    assert not any("Change notice" in t for t in texts(first))
    said = texts(second)
    assert "[superseded: record refs/K-C1 changed" in said[3]
    assert said[-2].startswith("Change notice:")
    assert "record refs/K-C1@1 is now version 2" in said[-2]
    t02 = next(e.seq for e in r.events("step.started") if e.step == "R-0001/extract[C1]/t02")
    reentries = [e.seq for e in r.events("task.entered") if e.payload["cause"] == "rebuild"]
    assert all(seq > t02 for seq in reentries)  # turn 2 was the same entry, not a re-entry


# --- replay and recovery -----------------------------------------------------------------------


def test_replay_loads_the_read_runs(make: Any) -> None:
    """F10: the read runs rebuild the choice; the run's own events alone do not."""
    r = make()
    run = settle(r, "resume")
    view = r.view()
    policy = r.setup.policy()
    assert runs_to_load(view, run) == frozenset({"R-0001", run})
    assert reentry_divergences(view, r.setup.tree, policy) == ()
    scoped = scoped_view(view, runs_to_load(view, run))
    assert reentry_divergences(scoped, r.setup.tree, policy) == ()
    alone = reentry_divergences(scoped_view(view, {run}), r.setup.tree, policy)
    assert alone and alone[0].recomputed.reason == "session_missing"
    # A changed number flips a mode, and the check reports it.
    stricter = replace(policy, resume=replace(policy.resume, max_stale_permille=0))
    (flipped,) = reentry_divergences(view, r.setup.tree, stricter)
    assert (flipped.instance, flipped.recomputed.reason) == ("extract[C1]", "mostly_stale")


def test_the_choice_is_recorded_once_and_never_recomputed(make: Any) -> None:
    """Assembly reads the record: a policy changed after entry changes no request."""
    from aksha.runtime.context.assembly import assemble_context
    from aksha.runtime.context.types import Assembled
    from aksha.runtime.stop_policy.types import Prep
    from aksha.runtime.tasks.types import InstanceRef

    r = make()
    run = settle(r, "resume")
    sent = r.requests_of(run, "extract[C1]")[0]
    ref = InstanceRef(run, "extract", "C1")
    other = ContextPolicy(resume=ResumePolicy(min_prior_turns=50))
    again = assemble_context(
        r.view(), r.setup.tree, ref, turn=1, inputs=r.setup.inputs(ref), prep=Prep(),
        policy=other, tools=r.setup.registry.offered(r.setup.task(ref), None),
        kinds=r.setup.kinds,
    )  # fmt: skip
    assert isinstance(again, Assembled)
    assert texts(again.request) == texts(sent)
    assert len(r.entries(run)) == 3  # one entry per re-entered instance


def _kill_after(d: Path, n: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests.support.killresume", str(d), str(n)],
        capture_output=True, text=True, cwd=ROOT, timeout=120, check=False,
    )  # fmt: skip


def test_sigkill_at_every_commit_of_a_resumed_rebuild_recovers(tmp_path: Path) -> None:
    """A real ``SIGKILL`` after each commit of the rebuild phase, then a restart.

    After every kill point: one rebuild run that ends complete, each re-entered instance
    entered once with the same recorded mode (extract resumes, its readers go fresh on
    their premise), each model step completed once with no step mismatch (the resumed
    context hashes the same after the restart), the rebuilt bundle built from the new
    record, and a recomputation of every choice that matches its record.
    """
    killed = 0
    for n in range(1, 80):
        d = tmp_path / f"k{n:02d}"
        d.mkdir()
        child = _kill_after(d, n)
        if COMPLETED in child.stdout:
            break
        assert child.returncode == -9, child.stderr[-2000:]
        killed += 1
        seen = recover(d)
        assert len(seen["rebuild_runs"]) == 1, (n, seen)
        assert seen["entries"] == [
            ("extract[C1]", "resume", None),
            ("compare[C1]", "fresh", "premise_changed"),
            ("bundle[C1]", "fresh", "premise_changed"),
        ], (n, seen)
        assert len(seen["models"]) == len(set(seen["models"])) == 4, (n, seen)
        assert seen["faults"] == [], (n, seen)
        assert [o for _, o in seen["ended"]] == ["complete", "complete"], (n, seen)
        assert seen["bundle"] == {"key": "C1", "from": "2"}, (n, seen)
        assert seen["divergences"] == (), (n, seen)
    else:
        raise AssertionError("the rebuild phase made 80 or more commits")
    assert killed >= 10
