"""Invariance and directional runs: one case in a base world and its variant, compared.

A test that knows one world cannot tell an agent that read the files from one that
memorised them. Metamorphic relations need no new labels: a change that must not move the
answer (invariance) and a change whose effect is known (directional).

Both compare outputs through the variant's rename map and path map: a base output's
content and its citations' files and quotes are carried into the variant's names, then
compared with the variant's outputs of the same kind. Offsets are not compared, because a
renamed file shifts them; each citation is already checked in its own world by
``citation.exists``.

The keys say where the variant changes the answer (:class:`KeyDelta`): a candidate whose
recommendation or comparison matrix differs between the two keys, and an entity the
variant adds, are in the changed set.

1. ``rel.invariant``: every output outside the changed set equals its base under the maps.
2. ``rel.same_verdict``: the case's verdict in the variant equals its verdict in the base.
3. ``rel.same_calls``: the agent made the same tool calls in the same order under the path
   map, the stand-in for the design's same-rule-IDs check, so an agent that branches on a
   name is caught even when its outputs happen to agree.
4. ``rel.directional``: invariance outside the changed set, and each output whose key
   section moved (a changed candidate's recommendation or matrix) differs from its base,
   so the change was followed and not ignored.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.grade import Check, CriterionResult, Outcome, failed, passed
from aksha.evals.key import AnswerKey
from aksha.evals.normalize import cell_state, enum_key
from aksha.evals.state import GradedState, Output
from aksha.evals.world import Renaming
from aksha.runtime.canonical import canonical_json


@dataclass(frozen=True)
class KeyDelta:
    """Where the variant's key differs from the base's, after renaming.

    ``changed`` holds the output keys left out of the unchanged set: every candidate whose
    keyed recommendation or matrix differs, and every keyed entity (a family) the variant
    adds. ``must_move`` holds the outputs that must differ from their base: the
    ``recommendation`` or ``matrix`` of a candidate whose matching key section differs.
    """

    changed: frozenset[str]
    must_move: frozenset[tuple[str, str]]


SECTION_KIND = {"recommendations": "recommendation", "comparison_matrix": "matrix"}


def key_delta(base: AnswerKey, variant: AnswerKey) -> KeyDelta:
    """The :class:`KeyDelta` between two keys."""
    changed: set[str] = set()
    move: set[tuple[str, str]] = set()
    for section, kind in SECTION_KIND.items():
        b = dict(base.data.get(section, {}))
        v = dict(variant.data.get(section, {}))
        for cand in set(b) | set(v):
            if _norm(section, b.get(cand)) != _norm(section, v.get(cand)):
                changed.add(cand)
                move.add((kind, cand))
    changed |= set(dict(variant.data.get("families", {}))) - set(
        dict(base.data.get("families", {}))
    )
    return KeyDelta(frozenset(changed), frozenset(move))


def changed_candidates(base: AnswerKey, variant: AnswerKey) -> set[str]:
    """The output keys outside the unchanged set (see :class:`KeyDelta`)."""
    return set(key_delta(base, variant).changed)


def _norm(section: str, value: Any) -> Any:
    if value is None:
        return None
    if section == "recommendations":
        return enum_key(dict(value).get("next_action", ""))
    return {
        f: {fam: cell_state(s) for fam, s in dict(fams).items()} for f, fams in dict(value).items()
    }


def _in_changed(out: Output, changed: set[str]) -> bool:
    return out.key in changed or any(out.key.startswith(f"{c}/") for c in changed)


def _shape(out: Output, maps: Renaming | None) -> str:
    content: Any = dict(out.content)
    cites = [(str(c.get("file")), str(c.get("exact"))) for c in out.citations]
    if maps is not None:
        content = maps.apply(content)
        cites = [(maps.path(f), maps.text(e)) for f, e in cites]
    return canonical_json({"kind": out.kind, "content": _strip(content), "cites": sorted(cites)})


def _strip(value: Any) -> Any:
    """Drop code-point offsets anywhere in the content: a rename shifts them."""
    if isinstance(value, Mapping):
        return {k: _strip(v) for k, v in value.items() if k not in ("start", "end")}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def _relations(state: GradedState, maps: Renaming | None) -> Counter[str]:
    out: Counter[str] = Counter()
    for r in state.relations:
        ex = None
        if r.excerpt is not None:
            f, e = str(r.excerpt.get("file")), str(r.excerpt.get("exact"))
            ex = (maps.path(f), maps.text(e)) if maps else (f, e)
        out[canonical_json({"type": r.type, "excerpt": ex, "label": r.label})] += 1
    return out


def compare_outputs(
    base: GradedState,
    variant: GradedState,
    maps: Renaming,
    changed: set[str],
    must_move: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[list[str], list[str]]:
    """(problems with the unchanged set, changed-set outputs that did not change)."""
    b = Counter(_shape(o, maps) for o in base.outputs.values() if not _in_changed(o, changed))
    v = Counter(_shape(o, None) for o in variant.outputs.values() if not _in_changed(o, changed))
    problems = [f"only in base: {s[:160]}" for s in (b - v).elements()]
    problems += [f"only in variant: {s[:160]}" for s in (v - b).elements()]
    rb, rv = _relations(base, maps), _relations(variant, None)
    problems += [f"relation only in base: {s[:160]}" for s in (rb - rv).elements()]
    problems += [f"relation only in variant: {s[:160]}" for s in (rv - rb).elements()]
    unmoved = []
    for kind, key in sorted(must_move):
        b_out, v_out = base.get(kind, key), variant.get(kind, key)
        if b_out is None or v_out is None:
            unmoved.append(f"{kind}/{key}: missing in {'base' if b_out is None else 'variant'}")
        elif _shape(b_out, maps) == _shape(v_out, None):
            unmoved.append(f"{kind}/{key}: unchanged although its key changed")
    return problems, unmoved


def _result(cid: str, c: Check) -> CriterionResult:
    return CriterionResult(cid, "relation", True, c.outcome, c.evidence)


def _calls(rec: EpisodeRecord, maps: Renaming | None) -> list[str]:
    out = []
    for step in rec.trace:
        if "tool" not in step:
            continue
        args = dict(step["args"])
        if maps is not None:
            args = {k: maps.path(v) if isinstance(v, str) else v for k, v in args.items()}
        out.append(canonical_json({"tool": step["tool"], "args": args}))
    return out


def invariance(
    base: EpisodeRecord,
    variant: EpisodeRecord,
    base_state: GradedState,
    variant_state: GradedState,
    maps: Renaming,
    *,
    changed: set[str],
    base_verdict: Outcome,
    variant_verdict: Outcome,
) -> list[CriterionResult]:
    """``rel.invariant``, ``rel.same_verdict`` and ``rel.same_calls`` (module docstring)."""
    problems, _ = compare_outputs(base_state, variant_state, maps, changed)
    excluded = sorted(o.ref for o in base_state.outputs.values() if _in_changed(o, changed))
    note = f"changed set left out: {excluded}" if excluded else "no output left out"
    inv = failed(*problems) if problems else passed(f"{len(base_state.outputs)} outputs", note)
    same = (
        passed(f"both {base_verdict}")
        if base_verdict == variant_verdict
        else failed(f"base {base_verdict}, variant {variant_verdict}")
    )
    bc, vc = _calls(base, maps), _calls(variant, None)
    calls = passed(f"{len(bc)} calls") if bc == vc else failed(*_first_diff(bc, vc))
    return [
        _result("rel.invariant", inv),
        _result("rel.same_verdict", same),
        _result("rel.same_calls", calls),
    ]


def _first_diff(a: list[str], b: list[str]) -> list[str]:
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        if x != y:
            return [f"call {i + 1}: base {x[:120]}", f"call {i + 1}: variant {y[:120]}"]
    return [f"base made {len(a)} calls, variant {len(b)}"]


def directional(
    base_state: GradedState,
    variant_state: GradedState,
    maps: Renaming,
    *,
    delta: KeyDelta,
) -> list[CriterionResult]:
    """``rel.directional``: unchanged outside the changed set, moved where the key moved."""
    changed = set(delta.changed)
    problems, unmoved = compare_outputs(base_state, variant_state, maps, changed, delta.must_move)
    if not delta.must_move:
        problems.append("the keys name no changed candidate; this is not a directional pair")
    c = (
        failed(*problems, *unmoved)
        if problems or unmoved
        else passed(f"changed set {sorted(changed)} moved; the rest held")
    )
    return [_result("rel.directional", c)]
