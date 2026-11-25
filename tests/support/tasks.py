"""Hand-built Task trees and logs for the Task model and scheduler tests (ADR-P15).

``TaskLog`` is a ``budgets.Log`` that also saves artifact rows, so a completion check sees
current versions, and commits scheduler facts only after the core catalogue accepts them
in the stop-decision boundary (U3), so a fact the runtime builds is one the store would
take. ``demo_tree`` is a neutral three-stage tree fanned out per item of a selection.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.projections.view import ArtifactsView, LogView, load_view
from aksha.runtime.storage.chain import content_hash
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.storage.rows import ArtifactRow, StoredArtifact
from aksha.runtime.tasks.completion import finish_verdict
from aksha.runtime.tasks.scheduler import schedule
from aksha.runtime.tasks.tree import CompiledTree, compile_tree
from aksha.runtime.tasks.types import CheckResult, InstanceRef, Read, Task, artifact_keys
from aksha.runtime.tasks.view import TaskView
from tests.support.budgets import RUN, Log
from tests.support.events import START

AGENT = "demo"
CORE = Catalogue(CORE_EVENT_TYPES)


def has_own(kind: str) -> Any:
    """A completion check: the instance's own slot of ``kind`` has a current version."""

    def check(view: TaskView) -> CheckResult:
        return CheckResult(ok=view.own(kind) is not None, message=f"save {kind} first")

    check.__qualname__ = f"has_own_{kind}"
    return check


def has(kind: str) -> Any:
    """A completion check: the un-keyed slot of ``kind`` has a current version."""

    def check(view: TaskView) -> CheckResult:
        return CheckResult(ok=view.latest(kind) is not None, message=f"save {kind} first")

    check.__qualname__ = f"has_{kind}"
    return check


SELECTION = artifact_keys("selection", "ids")


def demo_tasks(**extract: Any) -> tuple[Task, ...]:
    """``scope``, ``extract[per id]``, ``compare[per id]`` (needs extract), ``report``."""
    return (
        Task("scope", writes=("selection",), done=has("selection")),
        Task("extract", per=SELECTION, writes=("item",), done=has_own("item"), **extract),
        Task(
            "compare",
            per=SELECTION,
            needs=("extract",),
            reads=("item",),
            writes=("cmp",),
            done=has_own("cmp"),
        ),
        Task(
            "report",
            needs=("compare",),
            reads=(Read("cmp", keys="all"),),
            writes=("report",),
            done=has("report"),
        ),
    )


def demo_tree(**extract: Any) -> CompiledTree:
    """The compiled demo tree; ``extract`` sets fields on the ``extract`` Task."""
    return compile_tree(AGENT, demo_tasks(**extract))


def ref(path: str, key: str | None = None, run: str = RUN) -> InstanceRef:
    """An instance of ``path`` in the test run."""
    return InstanceRef(run, path, key)


class TaskLog(Log):
    """A ``Log`` whose artifact saves carry rows, and whose scheduler facts are checked."""

    def __init__(self, owner: str = "o") -> None:
        super().__init__(owner)
        self.rows: list[StoredArtifact] = []

    def view(self) -> LogView:
        seq = len(self.events)
        artifacts = ArtifactsView(tuple(self.rows), seq)
        return LogView(self.owner, seq, tuple(self.events), artifacts, complete=True)

    def save(
        self,
        kind: str,
        key: str | None,
        content: Mapping[str, Any],
        *,
        instance: str | None = None,
        full: bool = False,
    ) -> Event:
        name = kind if full else f"{AGENT}.{kind}"
        version = 1 + sum(
            1
            for e in self.events
            if e.type == "artifact.saved"
            and e.payload.get("kind") == name
            and e.payload.get("key") == key
        )
        slot = name if key is None else f"{name}/{key}"
        ref_text = f"{slot}@v{version}"
        digest = content_hash(content)
        payload = {
            "ref": ref_text, "kind": name, "key": key, "version": version,
            "content_hash": digest,
        }  # fmt: skip
        event = self.add("artifact.saved", payload, instance=instance)
        row = ArtifactRow(ref_text, name, key, version, content, digest, digest)
        self.rows.append(StoredArtifact(self.owner, event.seq, row))
        return event

    def select(self, *ids: str) -> Event:
        return self.save("selection", None, {"ids": list(ids)})

    def commit(self, facts: Iterable[NewEvent], boundary: str = "U3") -> list[Event]:
        """Append facts the core catalogue accepts in ``boundary``, as a store would."""
        out = []
        for fact in facts:
            CORE.check_boundary(fact.type, boundary)
            CORE.validate_payload(fact.type, fact.payload)
            out.append(self.add(fact.type, fact.payload, run=fact.run, instance=fact.instance))
        return out

    def pop(self, instance: str, popped: str = "done", *, run: str = RUN) -> Event:
        walk = 1 + sum(1 for e in self.events if e.type == "stop.decided")
        payload = {"walk": walk, "verdict": "continue", "candidate": "next", "pop": popped}
        CORE.validate_payload("stop.decided", payload)
        return self.add("stop.decided", payload, run=run, instance=instance)

    def veto(self, instance: str) -> Event:
        return self.decided("continue", instance=instance, candidate="finish")

    def raise_interrupt(self, instance: str, ident: str) -> Event:
        return self.add(
            "interrupt.raised", {"interrupt": ident, "kind": "input"}, instance=instance
        )

    def resolve(self, ident: str, answer: str = "yes") -> Event:
        return self.add("interrupt.resolved", {"interrupt": ident, "answer": answer})


class StoreLedger:
    """The same helpers as ``TaskLog``, committing to a real SQLite event store instead."""

    def __init__(self, path: Path, owner: str = "o") -> None:
        self.path = path
        self.owner = owner
        self.clock = FakeClock(START)
        self.store: EventStore = open_store(path, self.clock, writer=True, catalogue=CORE)

    def view(self) -> LogView:
        return load_view(self.store, self.owner)

    def _walk(self) -> int:
        return 1 + sum(1 for e in self.view().events if e.type == "stop.decided")

    def commit(self, facts: Iterable[NewEvent], boundary: str = "U3") -> list[Event]:
        facts = list(facts)
        self.clock.advance(1)
        return list(self.store.append(self.owner, boundary, facts)) if facts else []

    def save(self, kind: str, key: str | None, content: Mapping[str, Any]) -> Event:
        name = f"{AGENT}.{kind}"
        version = 1 + sum(
            1
            for e in self.view().events
            if e.type == "artifact.saved"
            and e.payload.get("kind") == name
            and e.payload.get("key") == key
        )
        slot = name if key is None else f"{name}/{key}"
        ref_text = f"{slot}@v{version}"
        digest = content_hash(content)
        payload = {
            "ref": ref_text, "kind": name, "key": key, "version": version,
            "content_hash": digest, "canonical_hash": digest,
        }  # fmt: skip
        row = ArtifactRow(ref_text, name, key, version, content, digest, digest)
        self.clock.advance(1)
        return self.store.append(
            self.owner, "U2", [NewEvent("artifact.saved", payload, run=RUN)], [row]
        )[0]

    def select(self, *ids: str) -> Event:
        return self.save("selection", None, {"ids": list(ids)})

    def pop(self, instance: str, popped: str = "done") -> Event:
        payload = {"walk": self._walk(), "verdict": "continue", "candidate": "next", "pop": popped}
        return self.commit([NewEvent("stop.decided", payload, run=RUN, instance=instance)])[0]

    def veto(self, instance: str) -> Event:
        payload = {"walk": self._walk(), "verdict": "continue", "candidate": "finish"}
        return self.commit([NewEvent("stop.decided", payload, run=RUN, instance=instance)])[0]

    def raise_interrupt(self, instance: str, ident: str) -> Event:
        payload = {"interrupt": ident, "kind": "input"}
        fact = NewEvent("interrupt.raised", payload, run=RUN, instance=instance)
        return self.commit([fact], boundary="U2")[0]

    def resolve(self, ident: str, answer: str = "yes") -> Event:
        payload = {"interrupt": ident, "answer": answer}
        return self.commit([NewEvent("interrupt.resolved", payload, run=RUN)], boundary="U4")[0]

    def reopen(self) -> StoreLedger:
        """Close the store and open it again, as a restarted process would."""
        self.store.close()
        return StoreLedger(self.path, self.owner)


Ledger = TaskLog | StoreLedger
Turn = Callable[[InstanceRef, Any], str]
"""One scripted model turn of a leaf instance: returns ``finish`` or ``wait``."""


def drive(
    ledger: Any,
    tree: CompiledTree,
    turn: Turn,
    *,
    answer: Callable[[Any], bool] | None = None,
    max_steps: int = 200,
    on_decision: Callable[[Any], None] | None = None,
) -> list[str]:
    """Run the tree as a stop policy would, sequentially; return the entered instances.

    Each step is one stop decision: the scheduler's facts, then the pick's turn and its
    finish request (DONE, a veto or FAIL). ``answer`` stands in for a person when nothing
    may run; it returns True when it answered something.
    """
    entered: list[str] = []
    for _ in range(max_steps):
        if on_decision is not None:
            on_decision(ledger)
        sched = schedule(ledger.view(), tree, RUN)
        committed = ledger.commit(sched.facts)
        new = [e.instance for e in committed if e.type == "task.entered"]
        entered += new
        if not sched.picks:
            if answer is not None and answer(ledger):
                continue
            return entered
        pick = sched.picks[0]
        if pick in sched.entered_done:
            ledger.pop(pick.instance, "done")
            continue
        if not tree.node(pick.task_path).is_leaf:
            if pick.instance in new:
                continue  # entered now; its children run next
            verdict = finish_verdict(ledger.view(), tree, pick)
            ledger.pop(pick.instance, "done" if verdict == "done" else "fail")
            continue
        if turn(pick, ledger) == "wait":
            continue
        verdict = finish_verdict(ledger.view(), tree, pick)
        if verdict == "veto":
            ledger.veto(pick.instance)
        else:
            ledger.pop(pick.instance, "done" if verdict == "done" else "fail")
    raise AssertionError("the tree did not settle")


def obedient(selection: tuple[str, ...]) -> Turn:
    """A scripted model that does each demo instance's work in one turn."""

    def turn(pick: InstanceRef, ledger: Any) -> str:
        if pick.task_path == "scope":
            ledger.select(*selection)
        elif pick.task_path == "extract":
            ledger.save("item", pick.key, {"of": pick.key})
        elif pick.task_path == "compare":
            ledger.save("cmp", pick.key, {"of": pick.key})
        elif pick.task_path == "report":
            ledger.save("report", None, {"rows": len(selection)})
        return "finish"

    return turn
