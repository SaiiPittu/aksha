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
``chain.jsonl``    every stored event's whole envelope with its ``prev_hash`` and ``hash``
``reads/``         each text the agent read, named by its hash
=================  ==========================================================================

The gate does not trust a flag for the store. :func:`check_chain` walks ``chain.jsonl``
from the store's genesis (recorded in ``meta.json`` as ``store_identity``) with the
runtime's own hashing, checks the last hash equals ``chain_head`` (the store's head when the
episode ended), that ``events.jsonl`` is exactly the chain's ``episode.*`` events, and that
``ledger.jsonl`` is exactly the chain's ``episode.effect_sent`` payloads in order. An
edited ``events.jsonl`` or ledger, a changed chain row, or a store whose rows no longer hash to its
head (an edited ``store.sqlite``) each make the episode ``ungradeable``. Like the store's
own chain this is tamper-evident, not tamper-proof: someone who rewrites the chain, the head
and ``meta.json`` together is caught only by an anchor held elsewhere.

A mutant (:meth:`EpisodeRecord.edited`) is a constructed record, not an edited bundle: it
is resealed with a chain over its edited events, marked ``resealed`` in its meta, so the
graders below the gate see it as an honest store holding those events.

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

from aksha.runtime.canonical import canonical_json, freeze, thaw
from aksha.runtime.events.envelope import Event
from aksha.runtime.storage.chain import CHAIN_VERSION, event_hash, genesis_hash

ENVELOPE = (
    "seq",
    "type",
    "payload",
    "at",
    "dedup_key",
    "owner",
    "run",
    "instance",
    "step",
    "session",
    "boot",
    "schema_version",
)
"""The envelope fields a ``chain.jsonl`` row carries: every field the chain hash covers."""


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
    chain: tuple[Mapping[str, Any], ...] = ()

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
        events_out = tuple(_renumber(ev))
        identity = dict(self.meta.get("store_identity") or MUTANT_IDENTITY)
        template = self.chain[0] if self.chain else {}
        chain = seal(events_out, identity, template)
        return replace(
            self,
            events=events_out,
            ledger=tuple(led),
            meta={
                **self.meta,
                "store_identity": identity,
                "chain_head": chain[-1]["hash"] if chain else identity["genesis_hash"],
                "resealed": True,
                **(meta or {}),
            },
            path=None,
            chain=tuple(chain),
        )


MUTANT_IDENTITY: Mapping[str, Any] = {
    "store_id": "st_" + "0" * 32,
    "created_at": "2026-01-01T00:00:00.000Z",
    "chain_version": CHAIN_VERSION,
    "genesis_hash": genesis_hash("st_" + "0" * 32, "2026-01-01T00:00:00.000Z"),
}
"""The identity a resealed record falls back to when its source carried none."""


def identity_json(
    store_id: str, created_at: str, chain_version: int, genesis: str
) -> dict[str, Any]:
    """A store identity as ``meta.json`` records it."""
    return {
        "store_id": store_id,
        "created_at": created_at,
        "chain_version": chain_version,
        "genesis_hash": genesis,
    }


def _event(row: Mapping[str, Any]) -> Event:
    return Event(
        type=str(row["type"]),
        payload=freeze(row["payload"]),
        run=row.get("run"),
        instance=row.get("instance"),
        step=row.get("step"),
        session=row.get("session"),
        dedup_key=row.get("dedup_key"),
        seq=int(row["seq"]),
        owner=str(row["owner"]),
        at=str(row["at"]),
        boot=int(row["boot"]),
        schema_version=int(row["schema_version"]),
    )


def chain_rows(events: Sequence[Event], prev: str) -> list[dict[str, Any]]:
    """``chain.jsonl`` rows for stored ``events``, hashed from ``prev`` as the store hashes."""
    rows: list[dict[str, Any]] = []
    for e in events:
        h = event_hash(e, prev)
        row = {name: thaw(getattr(e, name)) for name in ENVELOPE}
        rows.append({**row, "prev_hash": prev, "hash": h})
        prev = h
    return rows


def seal(
    events: Sequence[Mapping[str, Any]],
    identity: Mapping[str, Any],
    template: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """A chain over ``events`` (type and payload), its envelopes filled from ``template``."""
    stored = [
        _event(
            {
                "seq": i,
                "type": e["type"],
                "payload": e["payload"],
                "at": template.get("at", "2026-01-01T00:00:00.000Z"),
                "owner": template.get("owner", "episode:mutant"),
                "boot": template.get("boot", 1),
                "schema_version": template.get("schema_version", 1),
            }
        )
        for i, e in enumerate(events, start=1)
    ]
    return chain_rows(stored, str(identity["genesis_hash"]))


def check_chain(rec: EpisodeRecord) -> str | None:
    """Why the bundle's chain does not hold, or None when it does (module docstring)."""
    ident = rec.meta.get("store_identity")
    if not isinstance(ident, Mapping) or not rec.chain:
        return "the bundle carries no store chain"
    try:
        genesis = genesis_hash(
            str(ident["store_id"]), str(ident["created_at"]), int(ident["chain_version"])
        )
        if genesis != ident.get("genesis_hash"):
            return "the store identity does not hash to its genesis"
        prev = genesis
        for i, row in enumerate(rec.chain, start=1):
            if int(row["seq"]) != i:
                return f"chain row {i} has seq {row['seq']}"
            if row["prev_hash"] != prev:
                return f"chain row {i} does not link to the row before it"
            recomputed = event_hash(_event(row), prev)
            if recomputed != row["hash"]:
                return f"chain row {i} does not hash to its recorded hash"
            prev = recomputed
    except (KeyError, TypeError, ValueError) as e:
        return f"a chain row is malformed: {type(e).__name__}: {e}"
    if prev != rec.meta.get("chain_head"):
        return f"the chain ends at {prev}, the store's head was {rec.meta.get('chain_head')}"
    projected = _renumber(
        [
            {"seq": 0, "type": r["type"], "payload": r["payload"]}
            for r in rec.chain
            if str(r["type"]).startswith("episode.")
        ]
    )
    if canonical_json(projected) != canonical_json([dict(e) for e in rec.events]):
        return "events.jsonl is not the chain's episode events"
    sent = [e["payload"] for e in projected if e["type"] == "episode.effect_sent"]
    if canonical_json(sent) != canonical_json([dict(r) for r in rec.ledger]):
        return "ledger.jsonl is not the chain's deliveries"
    return None


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
    (folder / "chain.jsonl").write_text(_jsonl(rec.chain), encoding="utf-8")
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
        chain_path = folder / "chain.jsonl"
        chain = [json.loads(line) for line in _lines(chain_path)] if chain_path.is_file() else []
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
        chain=tuple(chain),
    )


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
