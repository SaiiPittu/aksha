"""The episode bundle: what an episode left behind, exported before any grader runs.

``Environment.finalize`` exports the episode's event store, the fixture receiver's ledger,
every source text the agent read (by hash) and a few facts about the run into a folder:

=================  ==========================================================================
``meta.json``      case, world, rollout, agent, mode, seed, the store's chain check, the
                   world lock check, steps and elapsed time
``events.jsonl``   the episode store's ``episode.*`` events in seq order (type and payload)
``ledger.jsonl``   the fixture receiver's rows, one per delivery
``trace.jsonl``    the normalised trace: tool calls with arguments and result hashes, no
                   times or IDs, so two identical rollouts hash the same
``reads/``         each text the agent read, named by its hash
=================  ==========================================================================

Graders read a bundle, never a live store, so a grader cannot change what it grades and a
mutant is made by editing a copy (:meth:`EpisodeRecord.edited`).
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from aksha.runtime.canonical import canonical_json


@dataclass(frozen=True)
class EpisodeRecord:
    """One exported episode. ``sources`` is the episode's world copy at its end."""

    meta: Mapping[str, Any]
    events: tuple[Mapping[str, Any], ...]
    ledger: tuple[Mapping[str, Any], ...]
    reads: Mapping[str, str]
    sources: Path
    path: Path | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def trace(self) -> list[dict[str, Any]]:
        """The normalised trace: each tool call's name, arguments, outcome and result hash."""
        out: list[dict[str, Any]] = []
        for e in self.events:
            p = e["payload"]
            if e["type"] == "episode.tool_called":
                out.append(
                    {
                        "tool": p["tool"],
                        "args": p["args"],
                        "ok": p["ok"],
                        "result_hash": p.get("result_hash"),
                        "error": p.get("error"),
                    }
                )
            elif e["type"] in ("episode.output_saved", "episode.relation_saved"):
                out.append({"save": e["type"], "payload": p})
        return out

    @property
    def trace_hash(self) -> str:
        """``sha256:`` over the normalised trace; ``k_effective`` counts distinct ones."""
        return "sha256:" + hashlib.sha256(canonical_json(self.trace).encode()).hexdigest()

    def edited(
        self,
        *,
        events: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
        ledger: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> EpisodeRecord:
        """A copy with its events, ledger or meta changed: how a mutant is made."""
        ev = copy.deepcopy([dict(e) for e in self.events])
        led = copy.deepcopy([dict(r) for r in self.ledger])
        if events is not None:
            ev = events(ev)
        if ledger is not None:
            led = ledger(led)
        return replace(
            self,
            events=tuple(_renumber(ev)),
            ledger=tuple(led),
            meta={**self.meta, **(meta or {})},
            path=None,
        )


def _renumber(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**e, "seq": i + 1} for i, e in enumerate(events)]


def _jsonl(rows: Sequence[Mapping[str, Any]]) -> str:
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows)


def write_bundle(rec: EpisodeRecord, folder: Path) -> Path:
    """Write ``rec`` into ``folder`` (created); returns the folder."""
    folder.mkdir(parents=True, exist_ok=True)
    try:
        sources = rec.sources.resolve().relative_to(folder.resolve().parent)
        sources_ref = f"../{sources.as_posix()}"
    except ValueError:
        sources_ref = str(rec.sources.resolve())
    meta = {**rec.meta, "sources": sources_ref}
    (folder / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    (folder / "events.jsonl").write_text(_jsonl(rec.events), encoding="utf-8")
    (folder / "ledger.jsonl").write_text(_jsonl(rec.ledger), encoding="utf-8")
    (folder / "trace.jsonl").write_text(_jsonl(rec.trace), encoding="utf-8")
    reads = folder / "reads"
    reads.mkdir(exist_ok=True)
    for h, text in rec.reads.items():
        (reads / h.removeprefix("sha256:")).write_text(text, encoding="utf-8")
    return folder


class BundleUnreadable(Exception):
    """A bundle folder that cannot be read back; the gate's ``bundle_unreadable``."""


def read_bundle(folder: Path) -> EpisodeRecord:
    """Read a bundle written by :func:`write_bundle`, or :class:`BundleUnreadable`."""
    try:
        meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        events = [json.loads(line) for line in _lines(folder / "events.jsonl")]
        ledger = [json.loads(line) for line in _lines(folder / "ledger.jsonl")]
        reads = {
            f"sha256:{p.name}": p.read_text(encoding="utf-8")
            for p in sorted((folder / "reads").glob("*"))
        }
    except (OSError, ValueError) as e:
        raise BundleUnreadable(f"{folder}: {e}") from None
    ref = str(meta.get("sources", ""))
    sources = (folder / ref).resolve() if ref.startswith("..") else Path(ref)
    return EpisodeRecord(
        meta=meta,
        events=tuple(events),
        ledger=tuple(ledger),
        reads=reads,
        sources=sources,
        path=folder,
    )


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
