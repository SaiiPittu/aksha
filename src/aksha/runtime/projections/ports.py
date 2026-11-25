"""The ``Reducer`` Protocol: ``name``, ``reads``, ``initial(args)`` and a pure ``apply``.

A reducer ignores every event type outside ``reads`` (ADR-P01 D6), so older code still
builds its projection over an event store with newer event types.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Protocol, TypeVar

from aksha.runtime.events.envelope import Event
from aksha.runtime.projections.view import ArtifactsView

S = TypeVar("S")


class Reducer(Protocol[S]):
    """Builds one projection by applying events, in seq order, to an initial state.

    ``apply`` is pure: no I/O, no clock, no randomness, and it never mutates ``state`` in
    place; it returns a new state. ``tests/architecture`` checks the runtime's reducers.
    """

    @property
    def name(self) -> str:
        """The projection's name, part of the projection cache key (``task_status``)."""
        ...

    @property
    def reads(self) -> frozenset[str]:
        """The event types ``apply`` is called with; every other type is skipped."""
        ...

    def initial(self, args: Hashable) -> S:
        """The state before any event, for these arguments (an instance path, a run ID)."""
        ...

    def apply(self, state: S, event: Event, artifacts: ArtifactsView) -> S:
        """The state after ``event``. ``artifacts`` shows the rows committed up to it."""
        ...
