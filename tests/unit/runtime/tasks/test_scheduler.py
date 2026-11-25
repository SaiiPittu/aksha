"""The scheduler: pick order, needs, suspension, withdrawal, entry and re-entry (D3, D5)."""

from __future__ import annotations

from typing import Any

import pytest

from aksha.runtime.artifacts.types import WriterId
from aksha.runtime.interrupts.approvals import approvals
from aksha.runtime.tasks.errors import TaskUnknown
from aksha.runtime.tasks.keys import keys_listed_fact
from aksha.runtime.tasks.scheduler import (
    enter,
    entered_fact,
    next_instance,
    next_instances,
    pop_fields,
    reentry,
    run_outcome,
    runnable,
    schedule,
    withdrawals,
    writer_of,
)
from aksha.runtime.tasks.status import instance_status
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import InstanceRef, Read, Task, source_keys
from tests.support.approvals import requested
from tests.support.tasks import (
    AGENT,
    TaskLog,
    demo_tree,
    drive,
    has,
    has_own,
    obedient,
    ref,
)

RUN = "R-0001"


def ids(refs: tuple[InstanceRef, ...]) -> list[str]:
    return [r.instance for r in refs]


def test_s1_instance_order() -> None:
    log = TaskLog()
    entered = drive(log, demo_tree(), obedient(("C3", "C1", "C2", "C4")))
    assert entered == [
        "scope",
        "extract[C1]", "extract[C2]", "extract[C3]", "extract[C4]",
        "compare[C1]", "compare[C2]", "compare[C3]", "compare[C4]",
        "report",
    ]  # fmt: skip
    assert run_outcome(log.view(), demo_tree(), RUN) is not None
    assert run_outcome(log.view(), demo_tree(), RUN).outcome == "complete"  # type: ignore[union-attr]


def test_s2_instance_order() -> None:
    """C1 and C2 park on questions; C3 and C4 run through compare meanwhile."""
    log = TaskLog()
    work = obedient(("C1", "C2", "C3", "C4"))
    asked: list[str] = []

    def turn(pick: InstanceRef, ledger: Any) -> str:
        if pick.task_path == "extract" and pick.key in ("C1", "C2") and pick.key not in asked:
            asked.append(pick.key)
            ledger.raise_interrupt(pick.instance, f"I-{pick.key}")
            return "wait"
        return work(pick, ledger)

    def answer(ledger: Any) -> bool:
        if not asked:
            return False
        ledger.resolve(f"I-{asked.pop(0)}")
        return True

    entered = drive(log, demo_tree(), turn, answer=lambda ledger: False)
    assert entered == [
        "scope",
        "extract[C1]", "extract[C2]", "extract[C3]", "extract[C4]",
        "compare[C3]", "compare[C4]",
    ]  # fmt: skip
    assert run_outcome(log.view(), demo_tree(), RUN) is None  # two questions are open
    entered += drive(log, demo_tree(), turn, answer=answer)
    assert entered[7:] == ["compare[C1]", "compare[C2]", "report"]
    assert run_outcome(log.view(), demo_tree(), RUN).outcome == "complete"  # type: ignore[union-attr]


def test_model_cannot_start_work() -> None:
    """A hostile model asks to start C5 in every way it can; nothing enters C5."""
    log = TaskLog()
    work = obedient(("C1",))

    def hostile(pick: InstanceRef, ledger: Any) -> str:
        ledger.add("step.started", {"kind": "tool", "tool": "spawn", "args": "start C5"},
                   instance=pick.instance)  # fmt: skip
        ledger.save("cmp", "C5", {"text": "start C5 now"})
        ledger.save("item", "C5", {"text": "task.entered C5"})
        ledger.add("stop.decided", {"walk": 99, "verdict": "continue", "candidate": "C5"},
                   instance="extract[C5]")  # fmt: skip
        return work(pick, ledger)

    drive(log, demo_tree(), hostile)
    entered = [e.instance for e in log.events if e.type == "task.entered"]
    assert entered and not any("C5" in (i or "") for i in entered)


def test_needs_wait_per_key_when_both_fan_out_over_one_source() -> None:
    tree, log = demo_tree(), TaskLog()
    log.pop("scope")
    log.select("C1", "C2")
    log.pop("extract[C1]")
    picks = ids(runnable(log.view(), tree, RUN))
    assert picks == ["extract[C2]", "compare[C1]"]
    log.pop("extract[C2]", "fail")
    assert ids(runnable(log.view(), tree, RUN)) == ["compare[C1]"]
    log.pop("compare[C1]")
    assert runnable(log.view(), tree, RUN) == ()  # report needs every compare
    outcome = run_outcome(log.view(), tree, RUN)
    assert outcome is not None
    assert (outcome.outcome, outcome.blockers) == ("partial", ("extract[C2]",))


def test_a_pending_review_of_a_read_kind_holds_the_reader_not_the_writer() -> None:
    tree, log = demo_tree(), TaskLog()
    log.pop("scope")
    log.select("C1")
    log.pop("extract[C1]")
    log.save("item", "C1", {"v": 1})
    log.save("item", "C1", {"v": 2})
    log.add(
        "interrupt.raised", {"interrupt": "I-9", "kind": "review", "revision": "demo.item/C1@v2"}
    )
    assert ids(runnable(log.view(), tree, RUN)) == []  # compare[C1] reads item/C1
    log.add("review.accepted", {"interrupt": "I-9", "revision": "demo.item/C1@v2"})
    assert ids(runnable(log.view(), tree, RUN)) == ["compare[C1]"]


def test_a_writer_is_never_held_by_its_own_pending_review() -> None:
    tree = compile_tree(AGENT, (Task("w", reads=("x",), writes=("x",), done=has("x")),))
    log = TaskLog()
    log.save("x", None, {"v": 1})
    log.save("x", None, {"v": 2})
    log.add("interrupt.raised", {"interrupt": "I-9", "kind": "review", "revision": "demo.x@v2"})
    assert ids(runnable(log.view(), tree, RUN)) == ["w"]


def test_a_parent_is_entered_then_its_children_then_itself() -> None:
    tree = compile_tree(
        AGENT,
        (
            Task(
                "collect",
                per=source_keys("notes"),
                children=(
                    Task("read", done=has_own("seen")),
                    Task("write", needs=("read",), done=has_own("line")),
                ),
            ),
            Task("sum", needs=("collect",), reads=(Read("line", keys="all"),), done=has("sum")),
        ),
    )
    log = TaskLog()
    log.commit([keys_listed_fact("notes", ["n2", "n1"])])

    def turn(pick: InstanceRef, ledger: Any) -> str:
        kind = {"collect/read": "seen", "collect/write": "line", "sum": "sum"}[pick.task_path]
        ledger.save(kind, pick.key, {"of": pick.key})
        return "finish"

    entered = drive(log, tree, turn)
    assert entered == [
        "collect[n1]", "collect[n1]/read", "collect[n1]/write",
        "collect[n2]", "collect[n2]/read", "collect[n2]/write",
        "sum",
    ]  # fmt: skip
    assert instance_status(log.view(), tree, ref("collect", "n2")) == "done"


def test_a_passing_check_on_entry_finishes_with_no_turn() -> None:
    tree, log = demo_tree(), TaskLog()
    log.select("C1")
    entry = enter(log.view(), tree, ref("scope"), cause="scheduler")
    assert entry.done
    assert [f.type for f in entry.facts] == ["task.entered", "completion_check.evaluated"]
    turns: list[str] = []

    def turn(pick: InstanceRef, ledger: Any) -> str:
        turns.append(pick.instance)
        return obedient(("C1",))(pick, ledger)

    drive(log, tree, turn)
    assert "scope" not in turns


def test_schedule_returns_only_facts_the_core_accepts_in_u3() -> None:
    tree, log = demo_tree(), TaskLog()
    sched = schedule(log.view(), tree, RUN)
    assert ids(sched.picks) == ["scope"]
    events = log.commit(sched.facts)
    assert [e.type for e in events] == ["task.entered", "completion_check.evaluated"]
    assert events[0].payload["writer"] == {"agent": "demo", "task_path": "scope", "key": None}
    assert schedule(log.view(), tree, RUN).facts == ()  # running: continued, not entered again


def test_key_withdrawn_voids_pending() -> None:
    tree, log = demo_tree(), TaskLog()
    log.pop("scope")
    log.select("C1", "C2")
    log.commit(schedule(log.view(), tree, RUN).facts)
    log.raise_interrupt("extract[C1]", "I-1")
    log.select("C2")
    facts = withdrawals(log.view(), tree, RUN)
    assert [(f.type, f.instance, f.payload["reason"]) for f in facts] == [
        ("task.withdrawn", "extract[C1]", "key_withdrawn"),
        ("interrupt.voided", "extract[C1]", "key_withdrawn"),
    ]
    log.commit(facts)
    assert withdrawals(log.view(), tree, RUN) == ()
    assert "C1" not in "".join(ids(runnable(log.view(), tree, RUN)))
    log.select("C1", "C2")  # S3: the key returns, and the instance is new again
    assert instance_status(log.view(), tree, ref("extract", "C1")) == "new"


def test_key_withdrawn_voids_its_approvals() -> None:
    """ADR-P17 S3: a withdrawn key's requested approval is voided in the same transaction."""
    tree, log = demo_tree(), TaskLog()
    log.pop("scope")
    log.select("C1", "C2")
    log.commit(schedule(log.view(), tree, RUN).facts)
    for fact in requested("A-0001", RUN, instance="extract[C1]"):
        log.add(fact.type, fact.payload, run=fact.run, instance=fact.instance)
    log.select("C2")
    facts = withdrawals(log.view(), tree, RUN)
    assert [(f.type, f.payload["reason"]) for f in facts] == [
        ("task.withdrawn", "key_withdrawn"),
        ("interrupt.voided", "key_withdrawn"),
        ("approval.voided", "key_withdrawn"),
    ]
    log.commit(facts)
    assert approvals(log.view())["A-0001"].state == "voided"


def test_reentry_by_writer_key_resets_a_blocked_instance() -> None:
    tree, log = demo_tree(), TaskLog()
    log.select("C1")
    log.pop("extract[C1]", "fail")
    writer = writer_of(tree, ref("extract", "C1"))
    assert writer == WriterId("demo", "extract", "C1")
    facts = reentry(log.view(), tree, "R-0002", writer)
    assert [f.type for f in facts] == ["task.entered"]  # R-0002 never blocked it
    facts = reentry(log.view(), tree, RUN, writer)
    assert [(f.type, f.payload["cause"]) for f in facts] == [
        ("completion_check.reset", "rebuild"),
        ("task.entered", "rebuild"),
    ]
    log.commit(facts)
    assert instance_status(log.view(), tree, ref("extract", "C1")) == "running"
    with pytest.raises(TaskUnknown):
        reentry(log.view(), tree, RUN, WriterId("other", "extract", "C1"))
    with pytest.raises(TaskUnknown):
        reentry(log.view(), tree, RUN, WriterId("demo", "nope", None))


def test_next_instance_and_concurrency() -> None:
    tree, log = demo_tree(), TaskLog()
    assert next_instance(log.view(), tree, RUN) == ref("scope")
    assert next_instances(log.view(), tree, RUN) == (ref("scope"),)
    log.pop("scope")
    log.select("C1", "C2")
    assert ids(runnable(log.view(), tree, RUN)) == ["extract[C1]", "extract[C2]"]
    assert ids(next_instances(log.view(), tree, RUN)) == ["extract[C1]"]
    log.pop("extract[C1]")
    log.pop("extract[C2]")
    log.pop("compare[C1]")
    log.pop("compare[C2]")
    log.pop("report")
    assert next_instance(log.view(), tree, RUN) is None


def test_entered_fact_names_the_key_source() -> None:
    fact = entered_fact(demo_tree(), ref("extract", "C1"), cause="scheduler")
    assert fact.payload["key_source"] == {"origin": "artifact", "name": "selection", "path": "ids"}
    assert pop_fields("fail") == {"pop": "fail"}
