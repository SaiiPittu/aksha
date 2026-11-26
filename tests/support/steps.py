"""Shared pieces for the durable execution tests: a simulated crash, scripted work, a log dump.

``Crash`` derives from ``BaseException``, not ``Exception``, so the step executor re-raises
it with nothing recorded, exactly as the process dying at that point would leave the log.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.types import Deadline, StepOutput
from aksha.runtime.policy.events import args_hash
from aksha.runtime.storage.ports import EventStore
from tests.support.events import TEST_TYPES


class Crash(BaseException):
    """The process dies here."""


HASH = args_hash({"q": "x"})
"""An arguments hash for steps whose arguments do not matter to the test."""


class Script:
    """Work functions that count their calls, so a test can assert what ran."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def ok(
        self, label: str, value: Mapping[str, Any] | None = None
    ) -> Callable[[Deadline], StepOutput]:
        def work(deadline: Deadline) -> StepOutput:
            self.calls.append(label)
            return StepOutput(dict(value or {"label": label}))

        return work

    def crash(self, label: str) -> Callable[[Deadline], StepOutput]:
        def work(deadline: Deadline) -> StepOutput:
            self.calls.append(label)
            raise Crash(label)

        return work

    def raises(self, label: str, *errors: BaseException) -> Callable[[Deadline], StepOutput]:
        """Raise each of ``errors`` on successive calls, then return ``ok``."""
        pending = list(errors)

        def work(deadline: Deadline) -> StepOutput:
            self.calls.append(label)
            if pending:
                raise pending.pop(0)
            return StepOutput({"label": label})

        return work


def log(store: EventStore, owner: str = "o") -> list[tuple[str, str | None, dict[str, Any]]]:
    """``(type, step, payload)`` for every event of ``owner``, in order."""
    return [(e.type, e.step, dict(e.payload)) for e in store.read(owner)]


def types(store: EventStore, owner: str = "o") -> list[str]:
    """The event types of ``owner``, in order."""
    return [e.type for e in store.read(owner)]


# --- probe types for the run classes whose event types later primitives declare ---------

# ``interrupt.raised``, ``interrupt.resolved`` and ``approval.requested`` are core types
# since invalidation (ADR-P05) became their first core writer, so they need no stand-in.


def recovery_catalogue() -> Catalogue:
    """The probe catalogue: the interrupt and approval types recovery reads are core now.

    ``run.stop_requested`` and ``interrupt.voided`` became core with cancellation (ADR-P18),
    and the interrupt and approval request types with invalidation (ADR-P05).
    """
    return Catalogue((*CORE_EVENT_TYPES, *TEST_TYPES))


def started_run(run: str, agent: str = "a", definition_hash: str | None = None) -> NewEvent:
    """A ``run.started`` for ``agent``, recording ``definition_hash`` when given."""
    payload: dict[str, Any] = {"agent": agent, "cause": "request"}
    if definition_hash is not None:
        payload["definition_hash"] = definition_hash
    return NewEvent("run.started", payload, run=run)


class FaultStore:
    """Wraps a store: records each append's boundary and types, and dies on a chosen one.

    ``die_on(boundary, types)`` returning True raises ``Crash`` before that append
    commits, as a process killed there would leave the log. Every other call passes
    through to the wrapped store.
    """

    def __init__(
        self,
        store: EventStore,
        die_on: Callable[[str, tuple[str, ...]], bool] | None = None,
    ) -> None:
        self.store = store
        self.die_on = die_on
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.store, name)

    def append(self, owner: str, boundary: str, events: Any, artifacts: Any = ()) -> Any:
        kinds = tuple(e.type for e in events)
        self.calls.append((boundary, kinds))
        if self.die_on is not None and self.die_on(boundary, kinds):
            raise Crash(f"killed before {boundary} {kinds} commits")
        return self.store.append(owner, boundary, events, artifacts)
