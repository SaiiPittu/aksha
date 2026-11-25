"""Small reducers the projection tests build, and a log generator with a seeded random."""

from __future__ import annotations

import random
from collections.abc import Hashable

from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.projections.view import ArtifactsView
from aksha.runtime.storage.ports import EventStore
from tests.support.events import artifact_row, probe, saved

ProbeState = tuple[tuple[str, int], ...]


class ProbeTrail:
    """Records every ``probe.noted`` and, for every save, whether its artifact row is visible."""

    name = "probe_trail"
    reads = frozenset({"probe.noted", "artifact.saved"})

    def initial(self, args: Hashable) -> ProbeState:
        return ()

    def apply(self, state: ProbeState, event: Event, artifacts: ArtifactsView) -> ProbeState:
        if event.type == "probe.noted":
            return (*state, ("n", int(event.payload["n"])))
        ref = str(event.payload["ref"])
        found = artifacts.get(ref)
        return (*state, (ref, 1 if found and found.event_seq <= event.seq else 0))


def fill(store: EventStore, owner: str, count: int, seed: int = 7, start: int = 0) -> None:
    """Append ``count`` boundaries of probes, saves and extension events to ``owner``.

    Probe numbers run from ``start + 1``, so calls with distinct ranges never share a key.
    """
    rng = random.Random(seed)
    n = start
    for _ in range(count):
        roll = rng.random()
        n += 1
        if roll < 0.2:
            ref = f"note/{owner}-{n}@v1"
            store.append(owner, "U2", [probe(n), saved(ref)], artifacts=[artifact_row(ref)])
        elif roll < 0.35:
            # Without the extension in the catalogue the roll appends nothing, so two stores
            # filled with one seed differ only by the extension's events.
            if "memory.recalled" in store.catalogue:
                store.append(owner, "U2", [NewEvent("memory.recalled", {"n": n})])
        else:
            store.append(owner, "U2", [probe(n)])
