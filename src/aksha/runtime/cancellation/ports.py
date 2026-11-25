"""The Protocol cancellation needs from outside: a unit of execution the host can end (D6)."""

from __future__ import annotations

from typing import Protocol


class KillableUnit(Protocol):
    """A tool body running where the host can end it: a process group in a sandbox (P12)."""

    def request_settle(self) -> None:
        """Ask the unit to stop on its own (SIGTERM to a process group)."""
        ...

    def terminate(self) -> None:
        """End the unit now (SIGKILL to a process group)."""
        ...

    def settled(self) -> bool:
        """Whether the unit has stopped running (or never started)."""
        ...

    def wait(self, seconds: float) -> bool:
        """Block up to ``seconds`` for the unit to stop; return :meth:`settled`."""
        ...
