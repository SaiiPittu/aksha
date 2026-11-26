"""Episodes as a projection (ADR-P09 D6) and the lineage closure and erasure hook (D4, D9)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.extensions.memory import Episode, episodes, erasure_inventory, used_by
from aksha.runtime.artifacts.hashing import content_hash
from aksha.runtime.projections.view import ArtifactsView, LogView
from aksha.runtime.storage.rows import ArtifactRow, StoredArtifact
from tests.support.budgets import Log

GEO = "memory.preference/research.geography"


class Saves(Log):
    """A log whose saves carry rows and recorded reads."""

    def __init__(self) -> None:
        super().__init__("o")
        self.rows: list[StoredArtifact] = []

    def save(self, ref: str, content: Mapping[str, Any], *, run: str = "R-0001",
             reads: tuple[str, ...] = ()) -> None:  # fmt: skip
        slot, _, version = ref.partition("@v")
        kind, _, key = slot.partition("/")
        digest = content_hash(content)
        payload = {
            "ref": ref, "kind": kind, "key": key or None, "version": int(version),
            "content_hash": digest,
            "reads": [{"kind": "artifact", "id": r.split("@")[0], "version": r.split("@")[1]}
                      for r in reads],
        }  # fmt: skip
        event = self.add("artifact.saved", payload, run=run)
        row = ArtifactRow(ref, kind, key or None, int(version), content, digest, digest)
        self.rows.append(StoredArtifact(self.owner, event.seq, row))

    def view(self) -> LogView:
        seq = len(self.events)
        return LogView(self.owner, seq, tuple(self.events), ArtifactsView(tuple(self.rows), seq),
                       complete=True)  # fmt: skip


def _story() -> Saves:
    log = Saves()
    log.add("run.started", {"agent": "scout", "cause": "request"}, run="R-0001")
    log.save(f"{GEO}@v1", {"subject": "research.geography", "text": "Only EU vendors.",
                          "source": {"cite": {"statement": "EX-0001"}}})  # fmt: skip
    log.save("scout.scope@v1", {"region": "EU"})
    log.add("run.ended", {"outcome": "complete", "reason": "done"}, run="R-0001")
    log.add("run.started", {"agent": "scout", "cause": "request"}, run="R-0002")
    log.add("step.completed", {"outcome": "ok", "tool": "recall",
                               "value": {"hits": [{"text": "Only EU vendors."}]}},
            run="R-0002")  # fmt: skip
    log.save("scout.scope@v2", {"region": "EU"}, run="R-0002", reads=(f"{GEO}@v1",))
    log.save("scout.report@v1", {"rows": 3}, run="R-0002", reads=("scout.scope@v2",))
    log.save("scout.other@v1", {"rows": 0}, run="R-0002")
    log.add("run.ended", {"outcome": "stopped", "reason": "person"}, run="R-0002")
    log.add("run.started", {"agent": "patent", "cause": "request"}, run="R-0003")
    log.add("run.ended", {"outcome": "complete", "reason": "done"}, run="R-0003")
    return log


def test_episodes_read_each_ended_run_back_and_store_nothing() -> None:
    log = _story()
    before = len(log.events)
    found = episodes(log.view(), frozenset({"scout"}))
    assert [(e.run_id, e.outcome) for e in found] == [("R-0001", "done"), ("R-0002", "stopped")]
    assert found[1] == Episode(
        run_id="R-0002", agent="scout", outcome="stopped", completed=(), blockers=(),
        outputs=("scout.scope@v2", "scout.report@v1", "scout.other@v1"),
    )  # fmt: skip
    assert len(log.events) == before


def test_used_by_is_the_lineage_closure_from_a_record() -> None:
    assert used_by(_story().view(), [f"{GEO}@v1"]) == ("scout.scope@v2", "scout.report@v1")


def test_the_erasure_inventory_lists_every_place_the_text_reached() -> None:
    inv = erasure_inventory(_story().view(), "memory.preference", "research.geography")
    assert inv.slot == GEO
    assert inv.versions == (f"{GEO}@v1",)
    assert inv.texts == ("Only EU vendors.",)
    assert [t for _, t in inv.events] == ["step.completed"]
    assert inv.dependents == ("scout.scope@v2", "scout.report@v1")
