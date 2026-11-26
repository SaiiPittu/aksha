"""Eval cases and suites: case files in TOML, labelled by kind and origin (D13-3).

A suite is a folder ``evals/suites/<suite>/`` holding ``suite.toml`` (the suite's name,
default rollouts and fault profiles) and one TOML file per case. A case names its worlds
and points into each world's key by dotted references (``expect``); it never copies an
expected value, so one case runs unchanged in a base world and its variants.

The three pairing rules (eval design section 2) are checked when a case is built:

1. ``origin = real_failure`` needs ``kind = bad`` and a source naming a bug or a run step.
2. ``origin = planted_trap`` needs a ``trap:`` source whose trap is in ``expect``; that the
   trap exists in every listed world is checked against the keys by :func:`check_against_keys`.
3. ``kind = control`` needs ``origin = hand_written``.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast, get_args

from aksha.evals.errors import CaseInvalid
from aksha.evals.faults import FaultProfile, FaultRule
from aksha.evals.key import AnswerKey

Kind = Literal["bad", "guard", "control"]
Origin = Literal["hand_written", "real_failure", "planted_trap"]
Category = Literal[
    "capability", "safety", "recovery", "limits", "change", "isolation", "extension", "robustness"
]
Relation = Literal["none", "invariance", "directional"]
SOURCE_PREFIXES = ("requirement:", "review:", "trap:", "bug:", "run:")


@dataclass(frozen=True)
class ChangeStep:
    """Apply ``change_set`` once an event matching ``after`` is recorded (``type:ref``)."""

    after: str
    change_set: str
    restart: bool = False


@dataclass(frozen=True)
class EvalCase:
    """One case: why it exists, where it came from, what it runs and how it is graded."""

    id: str
    kind: Kind
    origin: Origin
    source: str
    category: Category
    agents: tuple[str, ...]
    worlds: tuple[str, ...]
    opening: str = ""
    expect: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    should: tuple[str, ...] = ()
    judge: tuple[str, ...] = ()
    relation: Relation = "none"
    change_steps: tuple[ChangeStep, ...] = ()
    faults: str | None = None
    needs_action: bool = True
    k: int | None = None
    xfail: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        """Check the field values and the three pairing rules."""
        _enum(self.id, "kind", self.kind, Kind)
        _enum(self.id, "origin", self.origin, Origin)
        _enum(self.id, "category", self.category, Category)
        _enum(self.id, "relation", self.relation, Relation)
        if not self.agents or not self.worlds:
            raise CaseInvalid(message=f"{self.id}: a case names at least one agent and world")
        if not self.source.startswith(SOURCE_PREFIXES):
            raise CaseInvalid(
                message=f"{self.id}: source {self.source!r} starts with one of {SOURCE_PREFIXES}"
            )
        if self.origin == "real_failure" and (
            self.kind != "bad" or not self.source.startswith(("bug:", "run:"))
        ):
            raise CaseInvalid(
                message=f"{self.id}: a real_failure case is kind bad with a bug: or run: source"
            )
        if self.origin == "planted_trap":
            trap = self.trap_id
            if not self.source.startswith("trap:") or not trap:
                raise CaseInvalid(message=f"{self.id}: a planted_trap source is 'trap: <id>'")
            if f"traps.{trap}" not in self.expect:
                raise CaseInvalid(message=f"{self.id}: a planted_trap case expects traps.{trap}")
        if self.kind == "control" and self.origin != "hand_written":
            raise CaseInvalid(message=f"{self.id}: a control case is hand_written")
        overlap = set(self.criteria) & set(self.should)
        if overlap:
            raise CaseInvalid(message=f"{self.id}: {sorted(overlap)} are both must and should")
        if self.k is not None and self.k < 1:
            raise CaseInvalid(message=f"{self.id}: k is at least 1")

    @property
    def trap_id(self) -> str | None:
        """The trap a ``trap:`` source names, or None."""
        if not self.source.startswith("trap:"):
            return None
        return self.source.removeprefix("trap:").strip() or None

    @property
    def facts(self) -> tuple[str, ...]:
        """The fact IDs this case expects (``facts.KF-23`` gives ``KF-23``)."""
        return tuple(r.split(".")[1] for r in self.expect if r.startswith("facts."))


def _enum(case: str, name: str, value: object, literal: object) -> None:
    if value not in get_args(literal):
        raise CaseInvalid(message=f"{case}: {name} {value!r} is not one of {get_args(literal)}")


def check_against_keys(case: EvalCase, keys: Mapping[str, AnswerKey]) -> list[str]:
    """Problems with the case against each listed world's key (empty when it holds).

    A planted trap must exist in every world; an ``expect`` reference that a key lacks is
    reported too, but only as a warning line starting ``missing:``, because the design keeps
    such a criterion ``ungradeable`` (``key_ref_missing``) until the key gains the field.
    """
    problems: list[str] = []
    for world in case.worlds:
        key = keys.get(world)
        if key is None:
            problems.append(f"{case.id}: world {world} is not loaded")
            continue
        trap = case.trap_id
        if case.origin == "planted_trap" and trap not in key.traps:
            problems.append(f"{case.id}: trap {trap} is not in world {world}")
        problems.extend(
            f"missing: {case.id}: {world} has no {ref}" for ref in case.expect if not key.has(ref)
        )
    return problems


@dataclass(frozen=True)
class Suite:
    """A named set of cases with its default rollouts and fault profiles."""

    name: str
    cases: tuple[EvalCase, ...]
    k: int = 1
    description: str = ""
    faults: Mapping[str, FaultProfile] = field(default_factory=dict)
    path: Path | None = None

    def case(self, case_id: str) -> EvalCase:
        """The case ``case_id``, or :class:`CaseInvalid`."""
        for c in self.cases:
            if c.id == case_id:
                return c
        raise CaseInvalid("case_missing", f"suite {self.name} has no case {case_id}")

    @property
    def worlds(self) -> tuple[str, ...]:
        """Every world any case lists, in first-seen order."""
        seen: dict[str, None] = {}
        for c in self.cases:
            for w in c.worlds:
                seen.setdefault(w, None)
        return tuple(seen)


def _tuple(data: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = data.get(name, ())
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise CaseInvalid(message=f"{name} is a list of strings")
    return tuple(str(v) for v in value)


def case_from_toml(data: Mapping[str, Any], *, origin_file: str = "") -> EvalCase:
    """Build a case from one case file's parsed TOML."""
    try:
        steps = tuple(
            ChangeStep(
                after=str(s["after"]),
                change_set=str(s["change_set"]),
                restart=bool(s.get("restart", False)),
            )
            for s in data.get("change_steps", ())
        )
        k = data.get("k")
        return EvalCase(
            id=str(data["id"]),
            kind=cast(Kind, data["kind"]),
            origin=cast(Origin, data["origin"]),
            source=str(data["source"]),
            category=cast(Category, data.get("category", "capability")),
            agents=_tuple(data, "agents"),
            worlds=_tuple(data, "worlds"),
            opening=str(data.get("opening", "")),
            expect=_tuple(data, "expect"),
            criteria=_tuple(data, "criteria"),
            should=_tuple(data, "should"),
            judge=_tuple(data, "judge"),
            relation=cast(Relation, data.get("relation", "none")),
            change_steps=steps,
            faults=data.get("faults"),
            needs_action=bool(data.get("needs_action", True)),
            k=int(k) if k is not None else None,
            xfail=bool(data.get("xfail", False)),
            description=str(data.get("description", "")),
        )
    except KeyError as e:
        raise CaseInvalid(message=f"{origin_file}: missing field {e.args[0]}") from None


def _faults(data: Mapping[str, Any]) -> dict[str, FaultProfile]:
    out: dict[str, FaultProfile] = {}
    for name, body in dict(data.get("faults", {})).items():
        rules = tuple(
            FaultRule(
                tool=str(r["tool"]),
                code=str(r.get("code", "timeout")),
                probability_ppm=int(r.get("probability_ppm", 1_000_000)),
                path=r.get("path"),
            )
            for r in body.get("rules", ())
        )
        out[str(name)] = FaultProfile(name=str(name), rules=rules)
    return out


def load_suite(folder: Path) -> Suite:
    """Load ``folder/suite.toml`` and every other ``*.toml`` in ``folder`` as one case."""
    meta_path = folder / "suite.toml"
    if not meta_path.is_file():
        raise CaseInvalid("suite_missing", f"no suite.toml in {folder}")
    meta = tomllib.loads(meta_path.read_text(encoding="utf-8"))
    suite = dict(meta.get("suite", {}))
    faults = _faults(meta)
    cases: list[EvalCase] = []
    for path in sorted(folder.glob("*.toml")):
        if path.name == "suite.toml":
            continue
        case = case_from_toml(
            tomllib.loads(path.read_text(encoding="utf-8")), origin_file=path.name
        )
        if case.faults is not None and case.faults not in faults:
            raise CaseInvalid(message=f"{case.id}: fault profile {case.faults!r} is not defined")
        cases.append(case)
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise CaseInvalid(message=f"suite {folder.name}: duplicate case IDs")
    return Suite(
        name=str(suite.get("name", folder.name)),
        cases=tuple(cases),
        k=int(suite.get("k", 1)),
        description=str(suite.get("description", "")),
        faults=faults,
        path=folder,
    )
