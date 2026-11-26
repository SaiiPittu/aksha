"""Citation checks: the excerpt exists, it overlaps the keyed anchor tightly, edges carry one.

1. ``citation.exists``: every citation's quote is in its file, at its position when it gives
   one. A quote the file does not hold fails and sets ``fabricated``: a fabricated citation
   is worse than a missing one, and the report counts the two apart.
2. ``citation.overlaps_key``: for every fact the case expects, some citation overlaps the
   fact's anchor with a precision of at least 0.5 and a span of at most 600 code points or
   three times the anchor's length, so citing a whole file does not pass.
3. ``citation.edge_excerpts``: every domain relation carries an excerpt that exists.
"""

from __future__ import annotations

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase
from aksha.evals.criteria.common import locate, tight
from aksha.evals.grade import Check, Criterion, GradeContext, failed, passed

DOMAIN_EDGES = frozenset({"same_entity_as", "contradicts", "family_member_of", "precedes"})


def exists(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Every citation's quote occurs in its file (at its position when it gives one)."""
    bad: list[str] = []
    n = 0
    for where, cite in ctx.state.citations():
        n += 1
        if locate(ctx, cite) is None:
            bad.append(f"{where}: {cite.get('file')!s} lacks {str(cite.get('exact'))[:60]!r}")
    if bad:
        return failed(*bad, fabricated=True)
    return passed(f"{n} citations found in their files")


def overlaps_key(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Each expected fact with an anchor is cited tightly by some citation."""
    facts = [ctx.key.fact(f) for f in case.facts]
    anchored = [f for f in facts if f.anchor is not None]
    if not anchored:
        return passed("no anchored fact expected")
    located = []
    for _where, cite in ctx.state.citations():
        span = locate(ctx, cite)
        if span is not None:
            located.append(span)
    missing: list[str] = []
    notes: list[str] = []
    for fact in anchored:
        assert fact.anchor is not None
        best = 0
        ok = False
        for span in located:
            good, precision = tight(span, fact.anchor)
            best = max(best, precision)
            ok = ok or good
        if not ok:
            missing.append(f"{fact.id}: no tight citation (best precision {best / 1e6:.2f})")
        else:
            notes.append(f"{fact.id}: cited")
    if missing:
        return failed(*missing)
    return passed(*notes)


def edge_excerpts(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Every domain relation carries an excerpt found in its file."""
    bad: list[str] = []
    edges = [r for r in ctx.state.relations if r.type in DOMAIN_EDGES]
    for rel in edges:
        name = f"{rel.type} {rel.src} -> {rel.dst}"
        if rel.excerpt is None:
            bad.append(f"{name}: no excerpt")
        elif locate(ctx, rel.excerpt) is None:
            bad.append(f"{name}: excerpt not in {rel.excerpt.get('file')}")
    if bad:
        return failed(*bad)
    return passed(f"{len(edges)} domain edges, each with an excerpt")


CRITERIA = (
    Criterion("citation.exists", "citation", exists),
    Criterion("citation.overlaps_key", "citation", overlaps_key),
    Criterion("citation.edge_excerpts", "citation", edge_excerpts),
)
