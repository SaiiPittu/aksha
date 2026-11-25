"""Task values: the declared Task, key sources, reads and completion checks (ADR-P15).

The declared names: ``KeySource``, ``source_keys``, ``artifact_keys``, ``FromField``,
``from_field``, ``Read``, ``CheckResult``, ``Check`` and ``Task``. What the scheduler
computes from them: ``InstanceRef`` (one Task instance), ``InstanceStatus`` (a projection,
never stored), ``Evaluation`` (one completion check evaluation, joined with the children)
and ``SupervisorMove`` (what the supervisor rule chose). ``TaskView`` is in ``view.py``.

A ``Task`` is declared data the loader inspects; its completion check is a pure function
the runtime only calls. ``Task.model_aliases`` names the non-generation model aliases the
Task uses (ADR-P07 D2); every Task also has the implicit ``generate`` alias.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, get_args

if TYPE_CHECKING:
    from aksha.runtime.interrupts.types import DeclaredInterrupt
    from aksha.runtime.stop_policy.types import StopPolicyOptIn


@dataclass(frozen=True)
class CheckResult:
    """What a completion check returns. ``message`` is what the model reads on a veto."""

    ok: bool
    message: str = ""
    count: int = 0
    keys: tuple[str, ...] = ()
    clears: Literal["supervisor", "worker", "person"] | None = None


Check = Callable[[Any], CheckResult]
"""A pure function over a ``TaskView`` (typed ``Any`` until ADR-P15 builds ``TaskView``)."""


@dataclass(frozen=True)
class KeySource:
    """Where a fanned-out Task's keys come from: a source collection or an artifact field."""

    origin: Literal["source", "artifact"]
    name: str
    path: str | None = None
    unless: Check | None = None


def source_keys(collection: str) -> KeySource:
    """One Task instance per record of ``collection``."""
    return KeySource(origin="source", name=collection)


def artifact_keys(kind: str, path: str, unless: Check | None = None) -> KeySource:
    """One Task instance per key listed at ``path`` of the latest ``kind`` artifact."""
    return KeySource(origin="artifact", name=kind, path=path, unless=unless)


@dataclass(frozen=True)
class FromField:
    """Read the keys listed at ``path`` of a ``kind`` artifact the same Task also reads."""

    kind: str
    path: str


def from_field(kind: str, path: str) -> FromField:
    """A key binding for :class:`Read`: the keys listed in a field of another read."""
    return FromField(kind, path)


@dataclass(frozen=True)
class Read:
    """One declared read: a kind (full name, ``ns.kind``) and which of its keys."""

    kind: str
    keys: Literal["own", "all"] | FromField = "own"


@dataclass(frozen=True)
class Task:
    """A declared node of an agent's Task tree. ADR-P15's fields, plus ``model_aliases``."""

    id: str
    tools: tuple[str, ...] = ()
    reads: tuple[str | Read, ...] = ()
    writes: tuple[str, ...] = ()
    done: Check | None = None
    children: tuple[Task, ...] = ()
    per: KeySource | None = None
    needs: tuple[str, ...] = ()
    interrupt: DeclaredInterrupt | None = None
    extracts: bool = False
    stop_policies: tuple[StopPolicyOptIn, ...] = ()
    brief: str | None = None
    turn_limit: int = 10
    max_finish_vetoes: int = 3
    on_child_blocked: Literal["block", "partial"] = "block"
    finalizer: bool = False
    wakes_on: tuple[str, ...] = ()
    model_aliases: tuple[str, ...] = ()


InstanceStatus = Literal["new", "running", "suspended", "done", "blocked", "withdrawn"]
"""A Task instance's status (D6), computed by a reducer from the event store, never stored."""

INSTANCE_STATUSES: frozenset[str] = frozenset(get_args(InstanceStatus))

SETTLED: frozenset[InstanceStatus] = frozenset({"done", "blocked", "withdrawn"})
"""Statuses no turn changes: only a re-entry, a reset or a returning key leaves them."""

EntryCause = Literal["scheduler", "rebuild"]
"""Why ``task.entered`` was written: the scheduler's pick, or a rebuild run's re-entry."""

ResetCause = Literal["answer", "rebuild"]
"""Why a blocked instance was reset: a person's answer, or a rebuild run's re-entry."""

EvaluationCode = Literal[
    "", "check_failed", "check_raised", "children_pending", "child_blocked", "keys_pending"
]
"""Why an evaluation failed; empty when it passed."""

FinishVerdict = Literal["done", "veto", "fail"]
"""What a finish request becomes: DONE, a veto (one more turn) or FAIL (``blocked``)."""


@dataclass(frozen=True)
class InstanceRef:
    """One tracked Task instance: its run, its Task path and its key (None when not keyed).

    ``instance`` is the instance ID the step-ID grammar uses: the key rides on the keyed
    segment and its un-keyed children share it, ``collect[n1]/write`` (v1 keys only the
    top level, ``keyed_child_unsupported``).
    """

    run: str
    task_path: str
    key: str | None = None

    @property
    def instance(self) -> str:
        """``collect[n1]/write``, ``scope`` or ``extract[C1]``."""
        head, sep, rest = self.task_path.partition("/")
        if self.key is not None:
            head = f"{head}[{self.key}]"
        return f"{head}{sep}{rest}"

    @property
    def task_id(self) -> str:
        """The last segment of the path: the Task's own ``id``."""
        return self.task_path.rsplit("/", 1)[-1]

    def child(self, task_id: str) -> InstanceRef:
        """The instance of child Task ``task_id`` under this one; it shares this key."""
        return InstanceRef(self.run, f"{self.task_path}/{task_id}", self.key)

    @classmethod
    def parse(cls, run: str, instance: str) -> InstanceRef:
        """Read :attr:`instance` back: ``collect[n1]/write`` is ``("collect/write", "n1")``."""
        head, sep, rest = instance.partition("/")
        key: str | None = None
        if head.endswith("]") and "[" in head:
            head, _, key = head[:-1].partition("[")
        return cls(run, f"{head}{sep}{rest}", key)

    def __str__(self) -> str:
        """``R-0001/collect[n1]/write``."""
        return f"{self.run}/{self.instance}"


@dataclass(frozen=True)
class Evaluation:
    """One evaluation of an instance's completion, its own check joined with its children's.

    ``code`` says why it failed (empty when ``ok``); ``blockers`` names the blocked child
    instances a parent waits on (D2); ``clears`` is the check's tag (D7).
    """

    instance: str
    ok: bool
    message: str = ""
    count: int = 0
    keys: tuple[str, ...] = ()
    clears: Literal["supervisor", "worker", "person"] | None = None
    code: EvaluationCode = ""
    blockers: tuple[str, ...] = ()
    check: str = ""


SupervisorMoveName = Literal["turn", "push", "done", "hold", "fail"]


@dataclass(frozen=True)
class SupervisorMove:
    """What the supervisor rule chose for a parent instance on an entry or "next" candidate.

    ``turn``: the parent takes a turn of its own (``reason`` is ``wake`` or ``supervisor``);
    ``push``: the scheduler pushes ``child``; ``done``: its completion passes; ``hold``:
    every failing check waits on a person, so the run holds; ``fail``: the activation or
    no-progress limit bit (``reason`` names it).
    """

    move: SupervisorMoveName
    reason: str = ""
    child: InstanceRef | None = None
