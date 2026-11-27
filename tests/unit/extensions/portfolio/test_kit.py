"""The portfolio kit's pure parts: the audit sample, the goal, the recitation, commit refusals.

The views here are hand-built stand-ins with the four reads the kit makes (``latest``,
``all_latest``, ``child_status``, ``spent``); the loop itself is covered end to end in
``tests/integration/test_portfolio.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import pytest

from aksha.extensions.portfolio import (
    PortfolioSpec,
    Tier,
    audit_order,
    audit_sample,
    commit_round,
    deep_keys,
    goal,
    read_board,
    render,
    review,
    supervisor_task,
    turn_ceiling,
)
from aksha.extensions.portfolio.spec import REASON_CHARS
from aksha.runtime.api import CheckResult, Err, Ok, Task


@dataclass(frozen=True)
class Version:
    ref: str
    content: Mapping[str, Any]


@dataclass
class View:
    """A Task view stand-in: artifacts by (kind, key), statuses by Task id."""

    slots: dict[tuple[str, str | None], Version] = field(default_factory=dict)
    statuses: dict[str, dict[str | None, str]] = field(default_factory=dict)
    key: str | None = None

    def put(self, kind: str, key: str | None, content: Mapping[str, Any]) -> None:
        n = 1 + sum(1 for k, q in self.slots if k == kind and q == key)
        slot = kind if key is None else f"{kind}/{key}"
        self.slots[(kind, key)] = Version(f"fleet.{slot}@v{n}", content)

    def latest(self, kind: str, key: str | None = None) -> Version | None:
        return self.slots.get((kind, key))

    def all_latest(self, kind: str) -> Mapping[str | None, Version]:
        return {q: v for (k, q), v in self.slots.items() if k == kind}

    def child_status(self, task_id: str) -> Mapping[str | None, str]:
        return self.statuses.get(task_id, {})

    def spent(self, key: str | None = None) -> Mapping[str, int]:
        return {"model_turns": 1, "tool_calls": 2}


def _card(view: Any, key: str) -> Mapping[str, Any]:
    found = view.latest("card", key)
    return {} if found is None else {"score": found.content["score"]}


def _worth(view: Any, key: str) -> bool | None:
    found = view.latest("verdict", key)
    return None if found is None else bool(found.content["keep"])


SPEC = PortfolioSpec(
    agent="fleet", supervisor="lead", items=("listing", "items"), triage="triage",
    basis="card", deep=Tier("deep", "deep", effort=2, cap=4), card=_card, score="score",
    worth=_worth, budget_turns=8, batch_cap=3, audit_sample=2, supervise_above=3,
)  # fmt: skip
KEYS = tuple(f"SYN-I-{i}" for i in range(1, 7))


def carded(n: int = 6) -> View:
    view = View()
    view.put("listing", None, {"items": list(KEYS[:n])})
    for i, key in enumerate(KEYS[:n]):
        view.put("card", key, {"score": 10 - i})
    view.statuses["triage"] = {k: "done" for k in KEYS[:n]}
    return view


def decide(view: View, **states: str) -> None:
    for key, state in states.items():
        view.put("directive", key.replace("_", "-"), {
            "key": key.replace("_", "-"), "state": state, "reason": "r", "priority": 0,
            "basis": view.latest("card", key.replace("_", "-")).ref,  # type: ignore[union-attr]
        })  # fmt: skip


# --- the audit ---------------------------------------------------------------------------


def test_the_audit_order_is_seeded_and_the_same_for_the_same_keys() -> None:
    assert audit_order(KEYS, 0) == audit_order(reversed(KEYS), 0)
    assert audit_order(KEYS, 0) != audit_order(KEYS, 1)


def test_the_audit_sample_tops_up_and_never_forgets() -> None:
    first = audit_sample(KEYS[:4], (), SPEC)
    assert len(first) == 2 and set(first) <= set(KEYS[:4])
    again = audit_sample(KEYS, first, SPEC)
    assert again == sorted(first)  # full already: nothing new, nothing dropped
    bigger = audit_sample(KEYS, first, replace(SPEC, audit_sample=3))
    assert set(first) < set(bigger)


# --- the goal ------------------------------------------------------------------------------


def test_at_or_below_the_threshold_the_goal_needs_only_triage() -> None:
    view = carded(3)
    assert goal(SPEC)(view).ok
    assert deep_keys(view, SPEC) == frozenset(KEYS[:3])


def test_the_goal_tags_each_failure_with_who_clears_it_and_aggregates_keys() -> None:
    view = View()
    view.put("listing", None, {"items": list(KEYS)})
    found = goal(SPEC)(view)
    assert (found.ok, found.clears, found.count) == (False, "worker", 6)
    assert found.keys == KEYS[:5] and "and 1 more" in found.message
    view = carded()
    found = goal(SPEC)(view)
    assert (found.ok, found.clears) == (False, "supervisor")
    assert "6 items have no decision" in found.message


def test_a_decision_on_a_moved_basis_must_be_taken_again() -> None:
    view = carded()
    decide(view, **{k.replace("-", "_"): "dropped" for k in KEYS})
    view.put("round", None, {"audit": []})
    assert goal(SPEC)(view).ok
    view.put("card", KEYS[0], {"score": 1})  # a new triage result: the decision stays, flagged
    found = goal(SPEC)(view)
    assert not found.ok and "rest on a triage result that changed" in found.message
    assert view.latest("directive", KEYS[0]).content["state"] == "dropped"  # type: ignore[union-attr]


def test_an_open_audit_miss_reopens_drops_until_it_is_admitted() -> None:
    view = carded()
    decide(view, **{k.replace("-", "_"): "dropped" for k in KEYS})
    view.put("round", None, {"audit": [KEYS[4], KEYS[5]]})
    view.put("verdict", KEYS[4], {"keep": True})
    view.put("verdict", KEYS[5], {"keep": False})
    found = goal(SPEC)(view)
    assert not found.ok and found.message.startswith("re-open drops") and found.keys == (KEYS[4],)
    assert review(view, SPEC)["audit"] == {
        "sampled": [KEYS[4], KEYS[5]], "misses": [KEYS[4]], "miss_rate": "1 of 2",
        "miss_rate_ppm": 500_000,
    }  # fmt: skip
    decide(view, **{KEYS[4].replace("-", "_"): "admitted"})
    assert goal(SPEC)(view).ok


# --- the recitation ----------------------------------------------------------------------


def test_the_board_recites_counts_bounds_goal_and_capped_rows() -> None:
    view = carded()
    lines = render(replace(SPEC, page_rows=4))(view)
    board = read_board("\n".join(lines))
    assert board["counts"]["items"] == 6 and board["counts"]["undecided"] == 6
    assert board["bounds"]["deep_cap"] == 4 and board["bounds"]["deep_budget_turns"] == 8
    assert board["goal"].startswith("[supervisor] 6 items have no decision")
    assert [r["key"] for r in board["rows"]] == list(KEYS[:4])  # by score, highest first
    assert lines[-1] == "(2 more rows not shown)"
    assert {"turns", "calls", "basis_changed", "tier", "state"} <= set(board["rows"][0])


# --- commit_round's refusals ---------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    kind: str
    key: str | None
    version: int

    def __str__(self) -> str:
        slot = self.kind if self.key is None else f"{self.kind}/{self.key}"
        return f"{slot}@v{self.version}"


def ctx(view: View) -> Any:
    def latest(kind: str) -> tuple[Any, ...]:
        local = kind.split(".", 1)[1]
        return tuple(SimpleNamespace(ref=Ref(local, q, 1), content=v.content)
                     for (k, q), v in view.slots.items() if k == local)  # fmt: skip

    return SimpleNamespace(artifacts=SimpleNamespace(latest=latest))


NOTES = {"questions": [], "entries": [], "hypotheses": ""}
PLAN = {"goal": "g", "tier_rule": "t", "budget_split": "b"}


def commit(view: View, *changes: dict[str, Any], plan: bool = True) -> Any:
    args: dict[str, Any] = {"notes": NOTES, "directives": list(changes)}
    if plan:
        args["plan"] = PLAN
    return commit_round(SPEC, ctx(view), args)


def admit(*keys: str) -> dict[str, Any]:
    return {"keys": list(keys), "state": "admitted", "tier": "deep", "reason": "top"}


def drop(*keys: str, reason: str = "low") -> dict[str, Any]:
    return {"keys": list(keys), "state": "dropped", "reason": reason}


@pytest.mark.parametrize(
    ("changes", "plan", "code"),
    [
        ((admit(KEYS[0]),), False, "plan_required"),
        (({"keys": [KEYS[0]], "state": "admitted", "tier": "triage", "reason": ""},), True,
         "tier_not_admissible"),
        ((drop(KEYS[0], reason=" "),), True, "reason_required"),
        ((admit("SYN-I-99"),), True, "key_unknown"),
        ((admit(KEYS[0]), drop(KEYS[0])), True, "key_twice"),
        (({"keys": ["SYN-I-99"], "state": "admitted", "tier": "triage", "reason": ""},), True,
         "key_unknown"),
        ((drop("SYN-I-99", reason=" "),), True, "key_unknown"),
        ((admit(*KEYS[:4]),), True, "batch_cap"),
        ((admit(*KEYS[:3]), drop(*KEYS[3:])), True, "over_budget"),
    ],
)  # fmt: skip
def test_a_broken_bound_refuses_the_whole_round(
    changes: tuple[dict[str, Any], ...], plan: bool, code: str
) -> None:
    out = commit(carded(), *changes, plan=plan)
    assert isinstance(out.result, Err) and out.result.code == code
    assert out.saves == ()


def test_a_decision_on_an_item_without_triage_is_refused() -> None:
    view = carded()
    del view.slots[("card", KEYS[5])]
    out = commit(view, drop(KEYS[5]))
    assert isinstance(out.result, Err) and out.result.code == "not_triaged"


def test_the_tier_cap_is_checked_on_every_admitted_item() -> None:
    view = carded()
    decide(view, **{KEYS[0].replace("-", "_"): "admitted", KEYS[1].replace("-", "_"): "admitted",
                    KEYS[2].replace("-", "_"): "admitted"})  # fmt: skip
    args = {"notes": NOTES, "plan": PLAN, "directives": [admit(*KEYS[3:5])]}
    out = commit_round(replace(SPEC, budget_turns=100), ctx(view), args)
    assert isinstance(out.result, Err) and out.result.code == "tier_cap"


def test_a_good_round_saves_each_changed_directive_and_the_round_with_code_chosen_audit() -> None:
    view = carded()
    out = commit(view, admit(*KEYS[:2]), drop(*KEYS[2:]))
    assert isinstance(out.result, Ok)
    kinds = [s.kind for s in out.saves]
    assert kinds == ["fleet.directive"] * 6 + ["fleet.round"]
    round_ = out.saves[-1].content
    assert round_["audit"] == audit_sample(KEYS[2:], (), SPEC)  # the code's sample, not a choice
    assert all(len(s.content.get("reason", "")) <= REASON_CHARS for s in out.saves[:-1])
    directive = out.saves[0].content
    assert directive["basis"] == "card/SYN-I-1@v1"  # the version the decision was made on


# --- the deep tier's effort --------------------------------------------------------------


def _done(view: Any) -> CheckResult:
    return CheckResult(True)


def _deep(*limits: int) -> Task:
    steps = tuple(Task(f"s{i}", done=_done, turn_limit=n) for i, n in enumerate(limits))
    return Task("deep", children=steps)


def test_the_turn_ceiling_sums_the_limits_of_the_tasks_that_take_turns() -> None:
    assert turn_ceiling(_deep(6, 2, 2)) == 10
    assert turn_ceiling(Task("deep", done=_done, turn_limit=3)) == 3


def test_an_effort_below_the_deep_tasks_turn_limits_is_refused() -> None:
    triage = Task("triage", done=_done)
    with pytest.raises(ValueError, match="bound nothing"):
        supervisor_task(SPEC, triage=triage, deep=_deep(1, 2), brief="b.md")
    built = supervisor_task(SPEC, triage=triage, deep=_deep(1, 1), brief="b.md")
    assert [c.id for c in built.children] == ["triage", "deep"]
