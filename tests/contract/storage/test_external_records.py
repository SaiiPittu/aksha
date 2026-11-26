"""External record state reads the same over every storage adapter (ADR-P29, ADR-P01 D8).

The record facts ride on ``outbox.enqueued``, so they must survive each adapter's
canonical round trip unchanged, and the projection over them must be equal.
"""

from __future__ import annotations

from aksha.adapters.clock.fake import FakeClock
from aksha.runtime.artifacts.types import ArtifactRef
from aksha.runtime.outbox import facts
from aksha.runtime.outbox.outbox import enqueue, entries
from aksha.runtime.outbox.records import external_records
from aksha.runtime.outbox.types import EffectSpec
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.projections.view import load_view
from aksha.runtime.storage.ports import EventStore
from tests.support.systems import Ledger

FORM = EffectSpec("forms", ("/form", "/candidate"), "reconcile", record_fields=("/candidate",))


def test_two_revisions_of_one_record_round_trip(store: EventStore, clock: FakeClock) -> None:
    ledger = Ledger("forms")
    keys = []
    for form in ("p.form/SYN-C1@v1", "p.form/SYN-C1@v2"):
        out = enqueue(load_view(store, "o"), "write_form", {"form": form, "candidate": "C1"}, FORM)
        assert out.event is not None
        store.append("o", facts.ENQUEUE, (out.event,))
        OutboxWorker(store, "o", clock, {"forms": ledger}).drain()
        keys.append(out.entry.key)
    view = load_view(store, "o")
    entry = entries(view)[keys[1]]
    assert (entry.record, entry.sources) == ({"/candidate": "C1"}, ("p.form/SYN-C1@v2",))
    (rec,) = external_records(view)
    assert [r.key for r in rec.revisions] == keys
    assert rec.source_artifact == ArtifactRef.parse("p.form/SYN-C1@v2")
    assert rec.freshness is None  # no such version is saved in this store
