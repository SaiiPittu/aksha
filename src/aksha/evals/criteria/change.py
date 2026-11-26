"""Change checks: what a change step's change set must and must not move (eval design 3.1).

A change case applies a change set mid-episode (``episode.change_applied``) and is graded
against the change set's derived key, whose ``change`` section names output kinds by what
the change does to them:

1. ``change.stale_set``: every output of each ``change.stale`` kind is saved again, with
   new content, after the first change was applied (saving unchanged content records
   nothing, so a re-save is a real change).
2. ``change.unchanged_set``: no output of a ``change.unchanged`` kind is saved after it.
   This is the early-cutoff check: work the change could not reach keeps its version.
3. ``research.scope_change``: the research outputs carry the amended scope: the
   ``scope/all`` regions are the key's ``scope_change.regions`` and the review summary's
   per-theme counts inside the scope's geography (``themes_in_scope``) are the key's
   ``scope_change.in_scope_themes``.

An episode in which no change was applied fails the first two: there is nothing to grade
the change against, and passing would reward an agent that was never put through it.
"""

from __future__ import annotations

from collections.abc import Mapping

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase
from aksha.evals.grade import Check, Criterion, GradeContext, failed, passed
from aksha.evals.normalize import enum_key


def _changed_at(rec: EpisodeRecord) -> int | None:
    """The seq of the first ``episode.change_applied``, or None when none applied."""
    return next(
        (int(str(e["seq"])) for e in rec.events if e["type"] == "episode.change_applied"), None
    )


def _saved_after(rec: EpisodeRecord, at: int) -> dict[str, int]:
    """Each output ref saved after seq ``at``, with how many times."""
    out: dict[str, int] = {}
    for e in rec.events:
        if e["type"] == "episode.output_saved" and int(str(e["seq"])) > at:
            payload = e["payload"]
            assert isinstance(payload, Mapping)
            ref = str(payload["ref"])
            out[ref] = out.get(ref, 0) + 1
    return out


def _kinds(ctx: GradeContext, ref: str) -> list[str]:
    return [str(k) for k in ctx.key.resolve(ref)]


def stale_set(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Every output of a stale kind is saved again with new content after the change."""
    kinds = _kinds(ctx, "change.stale")  # a key with no change section: key_ref_missing
    at = _changed_at(rec)
    if at is None:
        return failed("no change was applied in this episode")
    after = _saved_after(rec, at)
    bad: list[str] = []
    notes: list[str] = []
    for kind in kinds:
        outs = ctx.state.of_kind(kind)
        if not outs:
            bad.append(f"{kind}: no output of this kind")
            continue
        missed = [o.ref for o in outs if o.ref not in after]
        if missed:
            bad.append(f"{kind}: {missed} not saved again after the change")
        else:
            notes.append(f"{kind}: {len(outs)} output(s) saved again")
    return failed(*bad) if bad else passed(*notes)


def unchanged_set(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """No output of an unchanged kind is saved after the change (early cutoff held)."""
    kinds = _kinds(ctx, "change.unchanged")  # a key with no change section: key_ref_missing
    at = _changed_at(rec)
    if at is None:
        return failed("no change was applied in this episode")
    after = _saved_after(rec, at)
    bad: list[str] = []
    kept = 0
    for kind in kinds:
        for o in ctx.state.of_kind(kind):
            if o.ref in after:
                bad.append(f"{o.ref}: saved again after the change, which cannot reach it")
            else:
                kept += 1
    if not kept and not bad:
        return failed(f"no output of {kinds}: nothing to show the change left alone")
    return failed(*bad) if bad else passed(f"{kept} output(s) kept their version")


def scope_change(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """The saved scope names the amended regions; in-scope theme counts match the key."""
    want_regions = sorted(enum_key(r) for r in ctx.key.resolve("scope_change.regions"))
    want_themes = {
        str(t): int(n) for t, n in dict(ctx.key.resolve("scope_change.in_scope_themes")).items()
    }
    bad: list[str] = []
    scope = ctx.state.get("scope", "all")
    if scope is None:
        bad.append("no scope/all output")
    else:
        got = sorted(enum_key(str(r)) for r in scope.content.get("regions", ()))
        if got != want_regions:
            bad.append(f"scope regions {got}, key {want_regions}")
    summary = ctx.state.get("review_summary", "all")
    got_themes = dict(summary.content.get("themes_in_scope", {})) if summary else {}
    for theme, n in sorted(want_themes.items()):
        if got_themes.get(theme) != n:
            bad.append(f"theme {theme}: {got_themes.get(theme)!r} in scope, key {n}")
    if bad:
        return failed(*bad)
    return passed(f"regions {want_regions}; {len(want_themes)} in-scope theme counts")


CRITERIA = (
    Criterion("change.stale_set", "state", stale_set),
    Criterion("change.unchanged_set", "state", unchanged_set),
    Criterion("research.scope_change", "state", scope_change),
)
