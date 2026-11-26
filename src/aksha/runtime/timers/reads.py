"""``TimeReads``: the time reads one attempt of a tool body made (ADR-P25 D4).

It depends on the artifact values alone, so ``tools.types`` can name it on the
``ToolContext`` without importing the rest of the timers package. The agent loop records
what it collected as the instance's reads with the step's result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aksha.runtime.artifacts.types import InputRef

CLOCK_ID = "clock"
"""The ID of the current date as a lineage input: ``time:clock@<ISO date>``."""


@dataclass
class TimeReads:
    """The time reads one attempt of a tool body made, in order, each once (D4).

    The agent loop gives a fresh one to each attempt, through ``ToolContext.time_reads``;
    ``observe_time`` and every read of ``ToolContext.run_date`` add to it.
    """

    _reads: list[InputRef] = field(default_factory=list)

    def observe(self, iso_date: str) -> InputRef:
        """Record that the body read ``iso_date``; the read as a lineage input."""
        read = InputRef("time", CLOCK_ID, iso_date)
        if read not in self._reads:
            self._reads.append(read)
        return read

    @property
    def reads(self) -> tuple[InputRef, ...]:
        """Every time read of the attempt, in order."""
        return tuple(self._reads)
