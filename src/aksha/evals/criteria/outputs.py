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

from collections.abc import Mapping
from typing import Any

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase
from aksha.evals.criteria.common import (
    all_citations,
    cites,
    competitor_output,
    family_label,
    field_of,
)
from aksha.evals.grade import Check, Criterion, GradeContext, failed, passed
from aksha.evals.key import KeyFact
from aksha.evals.normalize import cell_state, enum_key, is_unknown, name_key, price_of

FIELD_FOR_KIND: Mapping[str, str] = {
    "mechanism_step": "gates",
    "parameter": "quiet_window",
    "conception_record": "conception_date",
    "alternative_rejected": "rejected_alternative",
    "missing_detail": "quiet_window_rationale",
}
"""The extraction field each patent fact kind is saved under (provisional schema)."""
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
    """Every comparison cell's state equals the key's, with no extra families.

    A family is named by the key's label or by one of its priority numbers
    (:func:`~aksha.evals.criteria.common.family_label`).
    """
    want = dict(ctx.key.resolve("comparison_matrix"))
    bad: list[str] = []
    cells = 0
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
                if cell_state(row.get(fam, "")) != cell_state(state):
                    bad.append(f"{cand}.{feat}.{fam}: {row.get(fam)!r}, key {cell_state(state)}")
            extra = sorted(set(row) - {str(f).upper() for f in dict(fams)})
            if extra:
                bad.append(f"{cand}.{feat}: extra families {extra}")
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


def c1_extraction(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """The first keyed candidate's gates, window, conception date and rejected alternative.

    The candidate is the one the key's first mechanism-step fact is about (``C1`` in the
    stillroom worlds); each of its stated facts must come back as a cited value.
    """
    kinds = frozenset({"mechanism_step", "parameter", "conception_record", "alternative_rejected"})
    first = next(
        (
            f
            for f in ctx.key.facts.values()
            if f.kind == "mechanism_step" and f.about.get("candidate")
        ),
        None,
    )
    cand = str(first.about["candidate"]) if first is not None else "C1"
    out = ctx.state.get("extraction", cand)
    if out is None:
        return failed(f"no extraction/{cand} output")
    bad = []
    for fact in _candidate_facts(ctx, cand, kinds):
        name = FIELD_FOR_KIND[fact.kind]
        f = field_of(out, name)
        if f is None or is_unknown(f.get("value")):
            bad.append(f"{fact.id}: field {name} has no value")
            continue
        if fact.kind == "parameter" and not _same_quantity(f.get("value"), fact.value):
            bad.append(f"{fact.id}: {name} is {f.get('value')!r}, key {fact.value}")
        if fact.kind == "conception_record" and str(f.get("value")) != str(fact.value):
            bad.append(f"{fact.id}: {name} is {f.get('value')!r}, key {fact.value}")
        if not cites(ctx, list(f.get("citations", ())) + list(all_citations(out)), fact):
            bad.append(f"{fact.id}: {name} not cited at its anchor")
    return failed(*bad) if bad else passed(f"{cand} extraction matches")


def _same_quantity(got: Any, want: Any) -> bool:
    if isinstance(got, Mapping) and isinstance(want, Mapping):
        return got.get("value") == want.get("value") and enum_key(got.get("unit")) == enum_key(
            want.get("unit")
        )
    return bool(got == want)


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
        f = field_of(
            ctx.state.get("extraction", str(fact.about.get("candidate"))), FIELD_FOR_KIND[fact.kind]
        )
        if f is None or is_unknown(f.get("value")) or enum_key(f.get("status", "")) in ABSTAINING:
            bad.append(f"{fact.id}: stated value came back as an abstention")
    return failed(*bad) if bad else passed(f"{len(facts)} known values returned")


def abstention_count(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Questions, not-found entries and unknowns stay within the key's count plus slack."""
    want = int(ctx.key.resolve("counts.abstentions"))
    slack = int(ctx.key.counts.get("abstention_slack", 2))
    n = 0
    for out in ctx.state.outputs.values():
        for f in dict(out.content.get("fields", {})).values():
            if isinstance(f, Mapping) and (is_unknown(f.get("value")) or f.get("question")):
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
    Criterion("patent.matrix", "state", matrix, version="2"),
    Criterion("patent.next_action", "state", next_action),
    Criterion("patent.c1_extraction", "state", c1_extraction),
    Criterion("patent.priority_ranks", "state", priority_ranks),
    Criterion("patent.undated_disclosure", "state", undated_disclosure),
    Criterion("precision.known_values", "state", known_values, version="2"),
    Criterion("precision.abstention_count", "state", abstention_count),
)
