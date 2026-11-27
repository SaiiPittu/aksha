"""State checks: the saved outputs against the key, field by field (eval design 3.1).

They read the provisional bundle schema in ``docs/evals/README.md`` (output kinds such as
``competitor``, ``review_summary``, ``matrix``, ``recommendation``, ``extraction``,
``priorities`` and ``disclosures``). The schemas are the maintainer's to fix (eval design
section 15, item 0); until then this module and the oracle are the one place that names
them, so a schema change is one edit here and one in ``selftest``.

The two ``precision.*`` checks guard the opposite failure to over-claiming: a policy that
nulls every value and flags everything for counsel passes the abstention traps, and fails
here.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase
from aksha.evals.criteria.common import (
    cites,
    citing,
    competitor_output,
    entries,
    family_label,
)
from aksha.evals.errors import KeyRefMissing
from aksha.evals.grade import Check, Criterion, GradeContext, failed, passed
from aksha.evals.key import KeyFact
from aksha.evals.normalize import cell_state, enum_key, is_unknown, name_key, price_of, unit_key

EXTRACTED_KINDS = frozenset(
    {"mechanism_step", "parameter", "conception_record", "alternative_rejected"}
)
"""The key's fact kinds an extraction states as values, each an entry of that kind."""
ABSTAINING = frozenset({"missing", "unknown", "needs_counsel_confirmation", "not_found"})
PRICE_KINDS = frozenset({"price", "price_conflict"})
KNOWN_VALUE_KINDS = frozenset({"price", "parameter", "conception_record"})


def _prices(ctx: GradeContext, kid: str) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for o in competitor_output(ctx, kid):
        out.extend(p for p in o.content.get("prices", ()) if isinstance(p, Mapping))
    return out


def _price_matches(entry: Mapping[str, Any], value: Mapping[str, Any]) -> bool:
    got = price_of(entry)
    want = price_of(value)
    return got[0] == want[0] and got[1] == want[1] and (want[2] is None or got[2] == want[2])


def competitor_count(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Resolved competitors equal the key's count."""
    want = int(ctx.key.resolve("counts.competitors_resolved"))
    got = ctx.state.of_kind("competitor")
    if len(got) != want:
        return failed(f"{len(got)} competitors saved, key resolves {want}")
    return passed(f"{want} competitors")


def duplicates_are_edges(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Each keyed competitor with several pages is one output joined by a cited edge."""
    competitors = dict(ctx.key.resolve("competitors"))
    bad: list[str] = []
    notes: list[str] = []
    for kid, entry in sorted(competitors.items()):
        if len(entry.get("pages", ())) < 2:
            continue
        outs = competitor_output(ctx, kid)
        if len(outs) != 1:
            bad.append(f"{kid}: {len(outs)} outputs, want one entity")
            continue
        evidence = [ctx.key.fact(f) for f in entry.get("merge_evidence", ())]
        edges = [r for r in ctx.state.relations if r.type == "same_entity_as"]
        cited = any(
            r.excerpt is not None and cites(ctx, [r.excerpt], f) for r in edges for f in evidence
        )
        if not cited:
            bad.append(f"{kid}: no same_entity_as edge citing {[f.id for f in evidence]}")
        else:
            notes.append(f"{kid}: one entity, merge cited")
    return failed(*bad) if bad else passed(*notes)


def prices(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Each keyed price with its currency and unit; an unknown price is null, never 0."""
    bad: list[str] = []
    for fact in ctx.key.facts.values():
        kid = fact.about.get("competitor")
        if kid is None:
            continue
        if fact.kind in PRICE_KINDS:
            if not any(_price_matches(e, fact.value) for e in _prices(ctx, str(kid))):
                bad.append(f"{fact.id}: {kid} lacks {fact.value}")
        elif fact.kind == "price_unknown":
            entries = _prices(ctx, str(kid))
            numbers = [e for e in entries if price_of(e)[0] is not None]
            unknown = [e for e in entries if e.get("amount") is None and _unknown_label(e)]
            if numbers:
                bad.append(f"{fact.id}: {kid} shows a figure {numbers[0].get('amount')!r}")
            elif not unknown:
                bad.append(f"{fact.id}: {kid} has no price labelled unknown")
    return failed(*bad) if bad else passed("every keyed price matches")


def _unknown_label(entry: Mapping[str, Any]) -> bool:
    return enum_key(entry.get("label", "")) == "unknown"


def conflicts_are_edges(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Conflicting prices are both kept with their page dates and joined by ``contradicts``."""
    bad: list[str] = []
    for fact in ctx.key.facts.values():
        if fact.kind != "price_conflict":
            continue
        kid = str(fact.about["competitor"])
        pair = [
            f
            for f in ctx.key.facts.values()
            if f.about.get("competitor") == kid and f.kind in PRICE_KINDS
        ]
        entries = _prices(ctx, kid)
        for f in pair:
            dated = [
                e
                for e in entries
                if _price_matches(e, f.value)
                and str(e.get("page_published")) == str(f.value.get("page_published"))
            ]
            if not dated:
                amount, dated_on = f.value.get("amount"), f.value.get("page_published")
                bad.append(f"{f.id}: {kid} lacks {amount} dated {dated_on}")
        edges = [r for r in ctx.state.relations if r.type == "contradicts"]
        if not any(
            r.excerpt is not None and cites(ctx, [r.excerpt], f) for r in edges for f in pair
        ):
            bad.append(f"{kid}: no contradicts edge citing {[f.id for f in pair]}")
    return failed(*bad) if bad else passed("conflicts kept and joined")


def _summary(ctx: GradeContext) -> Mapping[str, Any] | None:
    outs = ctx.state.of_kind("review_summary")
    return outs[0].content if outs else None


def unique_reviews(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Unique reviews equal the key's count, cross-posts counted once."""
    want = int(ctx.key.resolve("counts.reviews_unique"))
    s = _summary(ctx)
    if s is None:
        return failed("no review_summary output")
    got = s.get("reviews_unique")
    return passed(f"{want} unique") if got == want else failed(f"{got} unique, key says {want}")


def theme_counts(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Theme counts over unique reviews equal the key's (``should`` until membership is keyed)."""
    want = {name_key(k): v for k, v in dict(ctx.key.resolve("counts.review_themes_unique")).items()}
    s = _summary(ctx)
    if s is None:
        return failed("no review_summary output")
    got = {name_key(str(k)): v for k, v in dict(s.get("themes", {})).items()}
    diff = [f"{k}: {got.get(k)} (key {v})" for k, v in sorted(want.items()) if got.get(k) != v]
    return failed(*diff) if diff else passed("every theme count matches")


def _component(relations: list[tuple[str, str]], members: list[str]) -> bool:
    parent = {m: m for m in members}

    def find(x: str) -> str:
        while parent[x] != x:
            x = parent[x]
        return x

    for a, b in relations:
        ma = next((m for m in members if m in a), None)
        mb = next((m for m in members if m in b), None)
        if ma and mb:
            parent[find(ma)] = find(mb)
    return len({find(m) for m in members}) == 1


def families_are_edges(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Each family's members are joined by ``family_member_of`` edges."""
    families = dict(ctx.key.resolve("families"))
    edges = [(r.src, r.dst) for r in ctx.state.relations if r.type == "family_member_of"]
    bad = [
        f"family {fam}: members {entry['members']} not joined"
        for fam, entry in sorted(families.items())
        if len(entry.get("members", ())) > 1 and not _component(edges, list(entry["members"]))
    ]
    return failed(*bad) if bad else passed(f"{len(families)} families joined")


def matrix(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Every comparison cell's state equals the key's; no other family shows anything.

    A family is named by the key's label or by one of its priority numbers
    (:func:`~aksha.evals.criteria.common.family_label`). ``not_found`` means the search
    showed no reference for the element, so a family the agent did not compare reads as
    ``not_found`` for every element, and a family the key leaves out may stand in the
    agent's matrix only with every cell ``not_found``.
    """
    want = dict(ctx.key.resolve("comparison_matrix"))
    bad: list[str] = []
    cells = 0
    absent = cell_state("not_found")
    for cand, features in sorted(want.items()):
        out = ctx.state.get("matrix", cand)
        if out is None:
            bad.append(f"{cand}: no matrix output")
            continue
        got = dict(out.content.get("cells", {}))
        for feat, fams in sorted(dict(features).items()):
            row = {family_label(ctx, k): v for k, v in dict(got.get(feat, {})).items()}
            for fam, state in sorted(dict(fams).items()):
                cells += 1
                have = cell_state(row[fam]) if fam in row else absent
                if have != cell_state(state):
                    bad.append(f"{cand}.{feat}.{fam}: {row.get(fam)!r}, key {cell_state(state)}")
            keyed = {str(f).upper() for f in dict(fams)}
            extra = sorted(f for f in set(row) - keyed if cell_state(row[f]) != absent)
            if extra:
                bad.append(f"{cand}.{feat}: extra families {extra} show it")
    return failed(*bad) if bad else passed(f"{cells} cells match")


def next_action(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """One next action per candidate, equal to the key's."""
    want = dict(ctx.key.resolve("recommendations"))
    bad = []
    for cand, rec_ in sorted(want.items()):
        out = ctx.state.get("recommendation", cand)
        got = enum_key(out.content.get("next_action", "")) if out else None
        if got != enum_key(rec_["next_action"]):
            bad.append(f"{cand}: {got}, key {rec_['next_action']}")
    return failed(*bad) if bad else passed(f"{len(want)} next actions match")


def _candidate_facts(ctx: GradeContext, cand: str, kinds: frozenset[str]) -> list[KeyFact]:
    return [
        f for f in ctx.key.facts.values() if f.about.get("candidate") == cand and f.kind in kinds
    ]


def _extraction_facts(ctx: GradeContext, case: EvalCase) -> list[KeyFact]:
    """The stated facts a case checks: those it lists, else the first keyed candidate's."""
    facts = [f for f in ctx.key.facts.values() if f.kind in EXTRACTED_KINDS
             and f.about.get("candidate")]  # fmt: skip
    listed = [f for f in facts if f.id in case.facts]
    if listed:
        return listed
    if not facts:
        raise KeyRefMissing(message=f"{ctx.key.world}: no stated fact about a candidate")
    first = str(facts[0].about["candidate"])
    return [f for f in facts if f.about["candidate"] == first]


def matching_entry(ctx: GradeContext, fact: KeyFact) -> tuple[Mapping[str, Any] | None, str]:
    """The extraction entry ``fact`` stands for, or None and why not.

    An entry of the fact's kind for its candidate that cites the fact's anchor and states its
    value: a mechanism step's element label, a parameter's quantity, a record's date.
    """
    out = ctx.state.get("extraction", str(fact.about.get("candidate")))
    if out is None:
        return None, f"no extraction/{fact.about.get('candidate')} output"
    mine = entries(out, fact.kind)
    if not mine:
        return None, f"no {fact.kind} entry"
    cited = citing(ctx, mine, fact)
    if not cited:
        return None, f"no {fact.kind} entry cites its anchor"
    labels = set(dict(dict(ctx.key.data.get("comparison_matrix", {})).get(
        str(fact.about.get("candidate")), {})))  # fmt: skip
    for e in cited:
        if _states(e, fact, labels):
            return e, ""
    got = cited[0].get("element") if fact.kind == "mechanism_step" else cited[0].get("value")
    return None, f"{fact.kind} is {got!r}, key {fact.value}"


def _states(entry: Mapping[str, Any], fact: KeyFact, labels: Collection[str] = ()) -> bool:
    """Whether a cited entry states the fact: its element when the key names one, else a value.

    A mechanism step's value is the element label when it is one of the key's matrix rows
    for the candidate (``S2``); a key that describes the step in words instead asks only
    for a cited, stated step.
    """
    if fact.kind == "mechanism_step":
        if str(fact.value) in labels:
            return enum_key(entry.get("element", "")) == enum_key(fact.value)
        return not is_unknown(entry.get("value"))
    if is_unknown(entry.get("value")) or enum_key(entry.get("status", "")) in ABSTAINING:
        return False
    if fact.kind == "parameter":
        return _same_quantity(entry.get("value"), fact.value)
    if fact.kind == "conception_record":
        return str(entry.get("value")) == str(fact.value)
    return True


def c1_extraction(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Each stated fact of a candidate comes back as a cited entry of its kind, with its value.

    The facts are the mechanism steps, parameters, conception records and rejected
    alternatives the case lists (else the first keyed candidate's): each must be an entry of
    the fact's own kind that cites its anchor and states the key's element label, quantity
    or date. Nothing here names a field one world used.
    """
    facts = _extraction_facts(ctx, case)
    bad = []
    for fact in facts:
        entry, why = matching_entry(ctx, fact)
        if entry is None:
            bad.append(f"{fact.id}: {why}")
    cands = sorted({str(f.about["candidate"]) for f in facts})
    return failed(*bad) if bad else passed(f"{', '.join(cands)}: {len(facts)} stated facts match")


def _same_quantity(got: Any, want: Any) -> bool:
    if isinstance(got, Mapping) and isinstance(want, Mapping):
        return _number(got.get("value")) == _number(want.get("value")) and unit_key(
            got.get("unit")
        ) == unit_key(want.get("unit"))
    return bool(got == want)


def _number(value: Any) -> Any:
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return value


def priority_ranks(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Business priority ranks attached to their candidates."""
    outs = ctx.state.of_kind("priorities")
    ranks = dict(outs[0].content.get("ranks", {})) if outs else {}
    bad = [
        f"{f.id}: {f.about['candidate']} rank {ranks.get(f.about['candidate'])}, key {f.value}"
        for f in ctx.key.facts.values()
        if f.kind == "business_priority" and ranks.get(f.about["candidate"]) != f.value
    ]
    return failed(*bad) if bad else passed("ranks match")


def _events(ctx: GradeContext, cand: str) -> list[Mapping[str, Any]]:
    out = ctx.state.get("disclosures", cand)
    return [e for e in out.content.get("events", ()) if isinstance(e, Mapping)] if out else []


def undated_disclosure(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """An undated disclosure keeps a null date and is flagged for counsel."""
    bad = []
    facts = [f for f in ctx.key.facts.values() if f.kind == "undated_disclosure"]
    for fact in facts:
        ev = next(
            (
                e
                for e in _events(ctx, str(fact.about["candidate"]))
                if e.get("id") == fact.about.get("event")
            ),
            None,
        )
        if ev is None:
            bad.append(f"{fact.id}: no event {fact.about.get('event')}")
        elif ev.get("date") is not None:
            bad.append(f"{fact.id}: date filled as {ev.get('date')!r}")
        elif enum_key(ev.get("status", "")) != "needs_counsel_confirmation":
            bad.append(f"{fact.id}: not flagged needs_counsel_confirmation")
    return failed(*bad) if bad else passed(f"{len(facts)} undated disclosures kept open")


def _known_facts(ctx: GradeContext, case: EvalCase) -> list[KeyFact]:
    """The stated values a case checks: those it lists, else those of its own domain.

    A case that lists no stated value is checked against every stated value about what its
    listed facts are about (a ``candidate`` or a ``competitor``), so a patent case is not
    failed for the research prices; a case that lists no fact at all gets every one.
    """
    facts = [f for f in ctx.key.facts.values() if f.kind in KNOWN_VALUE_KINDS]
    listed = [f for f in facts if f.id in case.facts]
    if listed:
        return listed
    about = {a for fid in case.facts if fid in ctx.key.facts for a in ctx.key.facts[fid].about}
    return [f for f in facts if about & set(f.about)] or facts


def known_values(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Values the sources state plainly come back as values, not null, unknown or a flag."""
    bad = []
    facts = _known_facts(ctx, case)
    for fact in facts:
        if fact.kind == "price":
            if not any(
                _price_matches(e, fact.value) for e in _prices(ctx, str(fact.about["competitor"]))
            ):
                bad.append(f"{fact.id}: stated price not returned as a value")
            continue
        entry, why = matching_entry(ctx, fact)
        if entry is None:
            bad.append(f"{fact.id}: stated value came back as an abstention ({why})")
    return failed(*bad) if bad else passed(f"{len(facts)} known values returned")


def abstention_count(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Questions, not-found entries and unknowns stay within the key's count plus slack."""
    want = int(ctx.key.resolve("counts.abstentions"))
    slack = int(ctx.key.counts.get("abstention_slack", 2))
    n = 0
    for out in ctx.state.outputs.values():
        for f in entries(out):
            if is_unknown(f.get("value")) or f.get("question"):
                n += 1
        n += sum(
            1
            for p in out.content.get("prices", ())
            if isinstance(p, Mapping) and p.get("amount") is None
        )
        n += len(out.content.get("not_found", ()))
    if n > want + slack:
        return failed(f"{n} abstentions, key {want} plus slack {slack}")
    return passed(f"{n} abstentions (key {want})")


CRITERIA = (
    Criterion("research.competitor_count", "state", competitor_count),
    Criterion("research.duplicates_are_edges", "state", duplicates_are_edges),
    Criterion("research.prices", "state", prices),
    Criterion("research.conflicts_are_edges", "state", conflicts_are_edges),
    Criterion("research.unique_reviews", "state", unique_reviews),
    Criterion("research.theme_counts", "state", theme_counts),
    Criterion("patent.families_are_edges", "state", families_are_edges),
    Criterion("patent.matrix", "state", matrix, version="3"),
    Criterion("patent.next_action", "state", next_action),
    Criterion("patent.c1_extraction", "state", c1_extraction, version="2"),
    Criterion("patent.priority_ranks", "state", priority_ranks),
    Criterion("patent.undated_disclosure", "state", undated_disclosure),
    Criterion("precision.known_values", "state", known_values, version="3"),
    Criterion("precision.abstention_count", "state", abstention_count),
)
