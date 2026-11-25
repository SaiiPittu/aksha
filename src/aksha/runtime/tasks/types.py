"""Task values: the declared Task, key sources, reads and completion checks (ADR-P15).

Names built so far: ``KeySource``, ``source_keys``, ``artifact_keys``, ``FromField``,
``from_field``, ``Read``, ``CheckResult``, ``Check`` and ``Task``. ``InstanceRef``,
``InstanceStatus`` and ``TaskView`` arrive with the scheduler.

A ``Task`` is declared data the loader inspects; its completion check is a pure function
the runtime only calls. ``Task.model_aliases`` names the non-generation model aliases the
Task uses (ADR-P07 D2); every Task also has the implicit ``generate`` alias.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

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
