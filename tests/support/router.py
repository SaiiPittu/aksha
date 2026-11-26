"""Shared pieces for the request router tests: two agents, a store, the router and its log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.events.envelope import Event
from aksha.runtime.execution.ids import LogIdSource
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.router.fake import PurposeClassifier
from aksha.runtime.router.router import Router
from aksha.runtime.router.types import RoutableAgent
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START

OWNER = "o1"
SESSION = "S-0001"

RESEARCH = RoutableAgent("research", "Research competitors and customers for a sensor product.")
PATENT = RoutableAgent("patent", "Prepare invention disclosures for a sensor idea.")
AGENTS = (RESEARCH, PATENT)

CLEAR = "Who are the competitors and customers for occupancy sensing?"
UNCLEAR = "Help me with the sensor thing"
DISCLOSURE = "Prepare a disclosure for the vibration idea"


class RouterBench:
    """A SQLite store, the fake classifier and a router a restart rebuilds."""

    def __init__(self, path: Path, model: object | None = None) -> None:
        self.path = path
        self.clock = FakeClock(START)
        self.store: EventStore = open_store(path, self.clock, writer=True)
        self.model = model if model is not None else PurposeClassifier()
        self.router = self._router()

    def _router(self) -> Router:
        return Router(AGENTS, self.model, LogIdSource(self.store))  # type: ignore[arg-type]

    def restart(self) -> Router:
        self.store.close()
        self.store = open_store(self.path, self.clock, writer=True)
        self.router = self._router()
        return self.router

    def view(self) -> LogView:
        return load_view(self.store, OWNER)

    def events(self) -> list[Event]:
        return list(self.store.read(OWNER))

    def types(self) -> list[str]:
        return [e.type for e in self.events()]

    def of(self, type_: str) -> list[dict[str, Any]]:
        return [dict(e.payload) for e in self.events() if e.type == type_]

    def close(self) -> None:
        self.store.close()
