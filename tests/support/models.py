"""Shared pieces for the model tests: a store with a started run, spans, payload scans."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.grounding.types import CitationSpan
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START

PAGE = "Pricing: 279 EUR per sensor, billed yearly."


def span(text: str = PAGE, span_id: str = "EX-0001", record: str = "pricing/acme") -> CitationSpan:
    return CitationSpan(span_id, "o", "fixtures", record, "v1", 0, len(text), text, "read")


class ModelHarness:
    """A SQLite store with runs started, and an executor that a restart rebuilds."""

    def __init__(self, path: Path, runs: tuple[str, ...] = ("R-0001",)) -> None:
        self.path = path
        self.clock = FakeClock(START)
        self.store: EventStore = open_store(path, self.clock, writer=True)
        for run in runs:
            self.store.append(
                "o", "U1", (NewEvent("run.started", {"agent": "a", "cause": "request"}, run=run),)
            )

    def executor(self) -> StepExecutor:
        return StepExecutor(self.store, "o", self.clock)

    def restart(self) -> StepExecutor:
        self.store.close()
        self.store = open_store(self.path, self.clock, writer=True)
        return self.executor()

    def start_run(self, run: str) -> None:
        self.store.append(
            "o", "U1", (NewEvent("run.started", {"agent": "a", "cause": "request"}, run=run),)
        )

    def log(self) -> list[tuple[str, str | None, dict[str, Any]]]:
        return [(e.type, e.step, dict(e.payload)) for e in self.store.read("o")]

    def close(self) -> None:
        self.store.close()


def floats_in(value: Any, path: str = "$") -> Iterator[str]:
    """The JSON path of every float in ``value``."""
    if isinstance(value, float):
        yield path
    elif isinstance(value, dict) or hasattr(value, "items"):
        for k, v in value.items():
            yield from floats_in(v, f"{path}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            yield from floats_in(v, f"{path}[{i}]")
