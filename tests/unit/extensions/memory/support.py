"""A fake artifact reader and a tool context for the memory tools' unit tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from aksha.runtime.api import ArtifactRef, ArtifactSnapshot, ToolContext
from aksha.runtime.artifacts.types import WriterId

AGENT = "scout"


@dataclass
class FakeReader:
    """Snapshots by ref; ``latest`` is each slot's highest version."""

    snaps: dict[str, ArtifactSnapshot] = field(default_factory=dict)

    def add(
        self,
        ref: str,
        content: Mapping[str, Any],
        *,
        agent: str = AGENT,
        freshness: str = "current",
        is_latest: bool = True,
    ) -> ArtifactSnapshot:
        snap = ArtifactSnapshot(
            ArtifactRef.parse(ref), content, WriterId(agent, "scope", None), "R-0001",
            freshness, is_latest,
        )  # fmt: skip
        self.snaps[ref] = snap
        return snap

    def latest(self, kind: str) -> tuple[ArtifactSnapshot, ...]:
        best: dict[str | None, ArtifactSnapshot] = {}
        for s in self.snaps.values():
            if s.ref.kind == kind:
                held = best.get(s.ref.key)
                if held is None or held.ref.version < s.ref.version:
                    best[s.ref.key] = s
        return tuple(best[k] for k in sorted(best, key=lambda k: k or ""))

    def get(self, ref: str) -> ArtifactSnapshot | None:
        return self.snaps.get(ref)


def ctx(reader: FakeReader | None = None, agent: str = AGENT) -> ToolContext:
    return ToolContext("o", agent, "scope", None, "R-0001/scope/t01/c01", 1,
                       artifacts=reader)  # fmt: skip


def record(subject: str, text: str, **cite: str) -> dict[str, Any]:
    return {"subject": subject, "text": text, "source": {"cite": cite or {"statement": "EX-0001"}}}
