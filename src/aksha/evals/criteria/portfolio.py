"""Portfolio checks: a supervisor Task over many candidates (ADR-P15 D7 S5, feature F-26).

They read the ``portfolio/*`` outputs the patent adapter saves when the candidates ran
under the supervisor (``aksha.evals.agents.patent._portfolio``, counted from the runtime's
own facts), and the world key's ``portfolio`` section. A run that was not supervised has
no such outputs, and each check fails saying so: a case lists these only for a world with
more candidates than the threshold.

1. ``portfolio.board_only``: the supervisor's model steps were shown no artifact and no
   document (``rendered`` is empty), it called only ``commit_round`` and ``finish``, it was
   offered only ``commit_round``, and its largest request stayed under the key's ceiling.
   What it read is the work board the runtime recites.
2. ``portfolio.tiers_save_calls``: the model turns spent are fewer than running the deep
   tier for every candidate would cost, at the deep turns per item this run measured.
3. ``portfolio.audit_catches_planted_drop``: the key's planted wrong drop was sampled by
   the audit, found worth the deep work (a miss), re-opened, and the review reports the
   miss rate; every drop carries a reason, and no candidate the key says to drop was
   admitted except by the cut the key expects.
4. ``portfolio.goal_holds``: the supervisor was done only after its goal passed, its goal
   failed at least once on its way there (the stop was held), every candidate ended
   admitted or dropped with a reason, and the selection Task started only after DONE.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase
from aksha.evals.grade import Check, Criterion, GradeContext, failed, passed

SUPERVISOR_TOOLS = frozenset({"commit_round", "finish"})
"""Every tool the supervisor may call: its one store tool and the implicit finish."""

DEFAULT_CEILING = 40_000
"""Characters a supervisor request may hold when the key names no ceiling."""


def _out(ctx: GradeContext, key: str) -> Mapping[str, Any] | None:
    found = ctx.state.get("portfolio", key)
    return None if found is None else found.content


def _key(ctx: GradeContext) -> Mapping[str, Any]:
    found: Mapping[str, Any] = ctx.key.resolve("portfolio")
    return found


def board_only(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """The supervisor was shown the board only (module doc, item 1)."""
    key = _key(ctx)
    sup = _out(ctx, "supervisor")
    if sup is None:
        return failed("no portfolio/supervisor output: the candidates did not run supervised")
    bad = []
    if int(sup.get("turns", 0)) < 1:
        bad.append("the supervisor took no turn")
    if sup.get("rendered"):
        bad.append(f"its requests showed artifacts: {list(sup['rendered'])[:5]}")
    called = set(dict(sup.get("tool_calls", {})))
    if not called <= SUPERVISOR_TOOLS:
        bad.append(f"it called {sorted(called - SUPERVISOR_TOOLS)}")
    if list(sup.get("tools_offered", ())) != ["commit_round"]:
        bad.append(f"it was offered {list(sup.get('tools_offered', ()))}")
    ceiling = int(key.get("supervisor_ceiling_chars", DEFAULT_CEILING))
    if int(sup.get("max_context_chars", 0)) > ceiling:
        bad.append(
            f"its largest request held {sup['max_context_chars']} characters, over {ceiling}"
        )
    if bad:
        return failed(*bad)
    return passed(f"{sup['turns']} supervisor turns, no artifact shown, tools {sorted(called)}, "
                  f"largest request {sup['max_context_chars']} characters")  # fmt: skip


def tiers_save_calls(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Fewer model turns than deep work for every candidate (module doc, item 2)."""
    _key(ctx)
    cost = _out(ctx, "cost")
    if cost is None:
        return failed("no portfolio/cost output: the candidates did not run supervised")
    turns = {k: int(v) for k, v in dict(cost.get("model_turns", {})).items()}
    items, deep_items = int(cost.get("items", 0)), int(cost.get("deep_items", 0))
    if not deep_items:
        return failed("no candidate got deep work, so nothing measures its cost")
    spent = sum(turns.values())
    per_item_x100 = turns.get("deep", 0) * 100 // deep_items
    everything = spent - turns.get("deep", 0) - turns.get("supervisor", 0)
    everything += turns.get("deep", 0) * items // deep_items
    line = (f"{spent} model turns spent (supervisor {turns.get('supervisor', 0)}, triage "
            f"{turns.get('triage', 0)}, deep {turns.get('deep', 0)} over {deep_items} of {items} "
            f"items at {per_item_x100 / 100:.2f} each, other {turns.get('other', 0)}); deep work "
            f"for every item would be about {everything}")  # fmt: skip
    if spent >= everything:
        return failed(line)
    return passed(
        line, f"saved {everything - spent} turns ({(everything - spent) * 100 // everything}%)"
    )


def audit_catches_planted_drop(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """The audit found the planted wrong drop and the goal re-opened it (module doc, item 3)."""
    key = _key(ctx)
    review = _out(ctx, "review")
    if review is None:
        return failed("no portfolio/review output: the candidates did not run supervised")
    planted = str(key["planted_wrong_drop"])
    audit = dict(review.get("audit", {}))
    bad = []
    if planted not in audit.get("sampled", ()):
        bad.append(f"{planted} was not in the audit sample {list(audit.get('sampled', ()))}")
    if planted not in audit.get("misses", ()):
        bad.append(f"the audit did not count {planted} as a miss")
    if planted not in review.get("admitted", ()):
        bad.append(f"{planted} is still dropped after the audit")
    if not audit.get("miss_rate"):
        bad.append("the review reports no miss rate")
    dropped = [dict(d) for d in review.get("dropped", ())]
    if any(not d.get("reason") for d in dropped):
        bad.append("a dropped candidate has no reason")
    should_admit = set(key.get("should_admit", ()))
    wrongly_dropped = sorted(should_admit & {str(d["key"]) for d in dropped})
    if bad:
        return failed(*bad)
    return passed(
        f"{planted} sampled, found worth the deep work, re-opened; miss rate {audit['miss_rate']}",
        f"{len(dropped)} drops with reasons; worth-keeping candidates still dropped: "
        f"{wrongly_dropped or 'none'}",
    )


def goal_holds(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """The supervisor's stop waited on its goal (module doc, item 4)."""
    key = _key(ctx)
    goal, review = _out(ctx, "goal"), _out(ctx, "review")
    if goal is None or review is None:
        return failed("no portfolio/goal output: the candidates did not run supervised")
    evaluations = [dict(e) for e in goal.get("evaluations", ())]
    turns = [dict(t) for t in goal.get("turns", ())]
    done = goal.get("done_seq")
    bad = []
    if done is None:
        bad.append("the supervisor's goal never passed")
    failing = [t for t in turns if not t.get("goal_ok") and t.get("clears") == "supervisor"]
    if not failing:
        bad.append("the goal never failed for the supervisor, so nothing shows it held the stop")
    if done is not None and any(t["seq"] > done for t in turns):
        bad.append("the supervisor took a turn after it was done")
    if done is not None and any(e["ok"] is False and e["seq"] > done for e in evaluations
                                if e.get("code") == "check_failed"):  # fmt: skip
        bad.append("the goal failed again after DONE")
    table = goal.get("table_entered_seq")
    if done is not None and (table is None or table < done):
        bad.append("the selection's table started before the supervisor was done")
    decided = set(review.get("admitted", ())) | {str(d["key"]) for d in review.get("dropped", ())}
    decided |= set(review.get("deferred", ()))
    every = set(dict(key.get("candidates", {})))
    if every and decided != every:
        bad.append(f"undecided at the end: {sorted(every - decided)[:5]}")
    if bad:
        return failed(*bad)
    reasons = sorted({str(t["goal"]).split(":")[0][:60] for t in failing})
    return passed(f"{len(failing)} failing goal evaluations before DONE ({'; '.join(reasons)}), "
                  f"then DONE; all {len(every)} candidates decided")  # fmt: skip


CRITERIA = (
    Criterion("portfolio.board_only", "state", board_only,
              description="the supervisor read only the work board"),
    Criterion("portfolio.tiers_save_calls", "state", tiers_save_calls,
              description="tiers spend fewer model turns than deep work for every item"),
    Criterion("portfolio.audit_catches_planted_drop", "trap", audit_catches_planted_drop,
              description="the audit sample catches the planted wrong drop and re-opens it"),
    Criterion("portfolio.goal_holds", "state", goal_holds,
              description="the supervisor finished only once every item was decided"),
)  # fmt: skip
