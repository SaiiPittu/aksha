"""Answer keys: the facts, traps and expected values a world's graders compare against.

A key is ``worlds/<w>/key.json`` (:mod:`aksha.data`). Agents and tools never read it: it sits beside
``sources/``, the only tool root, and :func:`aksha.evals.episode.guard_path` refuses any
path that resolves to it. Graders read it through :class:`AnswerKey`.

Every reference into a key is a dotted path (``counts.competitors_resolved``,
``facts.KF-23``, ``traps.T-DUP``, ``recommendations.C1.next_action``). :meth:`AnswerKey.resolve`
raises :class:`~aksha.evals.errors.KeyRefMissing` for a field the key lacks, which a grader
turns into an ``ungradeable`` result with cause ``key_ref_missing``, never a pass or a fail.

:func:`key_check` recomputes every anchor against the world's sources (the quote is found
once in its file and the position holds exactly that quote) and the counts a key can derive
from the files, so a hand-edited key that drifted from its world fails tier 0.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from aksha.evals.errors import KeyRefMissing
from aksha.evals.world import World


@dataclass(frozen=True)
class Anchor:
    """A W3C Web Annotation style anchor: quote selector plus code-point positions."""

    file: str
    prefix: str
    exact: str
    suffix: str
    start: int
    end: int


@dataclass(frozen=True)
class KeyFact:
    """One planted fact. ``about`` holds the candidate, competitor, family or person."""

    id: str
    kind: str
    value: Any
    expect: str | None
    anchor: Anchor | None
    about: Mapping[str, Any] = field(default_factory=dict)
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Trap:
    """One planted trap. ``check`` names the check; ``facts`` the facts it rests on."""

    id: str
    kind: str
    facts: tuple[str, ...]
    expect: str
    check: str
    raw: Mapping[str, Any] = field(default_factory=dict)


_ABOUT = ("candidate", "competitor", "family", "person", "event", "features")


def _fact(raw: Mapping[str, Any]) -> KeyFact:
    anchor = None
    if "file" in raw and "quote" in raw and "position" in raw:
        q, p = raw["quote"], raw["position"]
        anchor = Anchor(
            file=str(raw["file"]),
            prefix=str(q.get("prefix", "")),
            exact=str(q["exact"]),
            suffix=str(q.get("suffix", "")),
            start=int(p["start"]),
            end=int(p["end"]),
        )
    return KeyFact(
        id=str(raw["id"]),
        kind=str(raw["kind"]),
        value=raw.get("value"),
        expect=raw.get("expect"),
        anchor=anchor,
        about={k: raw[k] for k in _ABOUT if k in raw},
        raw=raw,
    )


def _trap(raw: Mapping[str, Any]) -> Trap:
    facts: list[str] = [str(f) for f in raw.get("facts", ())]
    if "fact" in raw:
        facts.append(str(raw["fact"]))
    return Trap(
        id=str(raw["id"]),
        kind=str(raw["kind"]),
        facts=tuple(facts),
        expect=str(raw.get("expect", "")),
        check=str(raw.get("check", f"trap.{raw['id']}")),
        raw=raw,
    )


@dataclass(frozen=True)
class AnswerKey:
    """A loaded key. ``data`` is the whole file; ``facts`` and ``traps`` are indexed by ID."""

    world: str
    frozen_today: date
    data: Mapping[str, Any]
    facts: Mapping[str, KeyFact]
    traps: Mapping[str, Trap]

    @property
    def counts(self) -> Mapping[str, Any]:
        """The ``counts`` section (empty when the key has none)."""
        return dict(self.data.get("counts", {}))

    def fact(self, fact_id: str) -> KeyFact:
        """The fact ``fact_id``, or :class:`KeyRefMissing`."""
        try:
            return self.facts[fact_id]
        except KeyError:
            raise KeyRefMissing(message=f"{self.world}: no fact {fact_id}") from None

    def resolve(self, ref: str) -> Any:
        """The value at a dotted reference; ``facts.<id>`` and ``traps.<id>`` index by ID."""
        head, _, rest = ref.partition(".")
        node: Any
        if head == "facts" and rest:
            fid, _, rest = rest.partition(".")
            node = self.fact(fid).raw
        elif head == "traps" and rest:
            tid, _, rest = rest.partition(".")
            if tid not in self.traps:
                raise KeyRefMissing(message=f"{self.world}: no trap {tid}")
            node = self.traps[tid].raw
        else:
            if head not in self.data:
                raise KeyRefMissing(message=f"{self.world}: no section {head!r}")
            node = self.data[head]
        for part in rest.split(".") if rest else ():
            if isinstance(node, Mapping) and part in node:
                node = node[part]
            else:
                raise KeyRefMissing(message=f"{self.world}: {ref} (no {part!r})")
        return node

    def has(self, ref: str) -> bool:
        """Whether :meth:`resolve` would find ``ref``."""
        try:
            self.resolve(ref)
        except KeyRefMissing:
            return False
        return True


def parse_key(data: Mapping[str, Any]) -> AnswerKey:
    """Build an :class:`AnswerKey` from a key file's parsed JSON."""
    facts = {f.id: f for f in (_fact(r) for r in data.get("facts", ()))}
    traps = {t.id: t for t in (_trap(r) for r in data.get("traps", ()))}
    return AnswerKey(
        world=str(data["world"]),
        frozen_today=date.fromisoformat(str(data["frozen_today"])),
        data=data,
        facts=facts,
        traps=traps,
    )


def load_key(world: World, path: Path | None = None) -> AnswerKey:
    """The world's key (or the key at ``path``, such as a change set's derived key)."""
    data = json.loads((path or world.key_path).read_text(encoding="utf-8"))
    return parse_key(data)


def read_source(world: World, rel: str) -> str:
    """A source file's or web page's raw text, for graders and ``key_check`` (never a tool)."""
    return world.file(rel).read_text(encoding="utf-8")


def key_check(world: World, key: AnswerKey) -> list[str]:
    """Every drift between the key and its world's sources; empty when the key holds.

    Checks each anchor (the file exists, the quote occurs exactly once, the position holds
    exactly the quote, prefix and suffix sit around it) and the counts derivable from the
    files: competitor pages, publications and review rows.
    """
    drifts: list[str] = []
    for fact in key.facts.values():
        a = fact.anchor
        if a is None:
            continue
        path = world.file(a.file)
        if not path.is_file():
            drifts.append(f"{fact.id}: file {a.file} is missing")
            continue
        text = path.read_text(encoding="utf-8")
        found = text.count(a.exact)
        if found != 1:
            drifts.append(f"{fact.id}: quote found {found} times in {a.file}")
        if text[a.start : a.end] != a.exact:
            drifts.append(f"{fact.id}: position {a.start}-{a.end} does not hold the quote")
        if a.prefix and not text[: a.start].endswith(a.prefix):
            drifts.append(f"{fact.id}: prefix does not precede the quote")
        if a.suffix and not text[a.end :].startswith(a.suffix):
            drifts.append(f"{fact.id}: suffix does not follow the quote")
    counts = key.counts
    derived = {
        "competitor_pages": _count_files(world, "competitors"),
        "publications": _count_files(world, "publications"),
        "review_rows": _count_reviews(world),
    }
    for name, value in derived.items():
        if name in counts and value is not None and counts[name] != value:
            drifts.append(f"counts.{name}: key says {counts[name]}, sources give {value}")
    return drifts


def _count_files(world: World, folder: str) -> int | None:
    path = world.sources / folder
    return sum(1 for p in path.iterdir() if p.is_file()) if path.is_dir() else None


def _count_reviews(world: World) -> int | None:
    path = world.sources / "reviews" / "reviews.json"
    if not path.is_file():
        return None
    return len(json.loads(path.read_text(encoding="utf-8")).get("reviews", ()))
