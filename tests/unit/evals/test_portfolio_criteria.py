"""The portfolio checks on hand-built outputs: each passes the oracle and a mutant kills it.

The stand-in context has the two reads the checks make: ``state.get(kind, key)`` for the
``portfolio/*`` outputs the patent adapter saves, and ``key.resolve("portfolio")``.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from aksha.evals.criteria import portfolio as p
from aksha.evals.errors import KeyRefMissing

KEY = {"planted_wrong_drop": "SYN-C-3", "should_admit": ["SYN-C-1", "SYN-C-3"],
       "candidates": {"SYN-C-1": {}, "SYN-C-2": {}, "SYN-C-3": {}, "SYN-C-4": {}}}  # fmt: skip
ORACLE: dict[str, Any] = {
    "supervisor": {
        "turns": 2,
        "rendered": [],
        "tool_calls": {"commit_round": 2, "finish": 2},
        "max_context_chars": 9_000,
        "tools_offered": ["commit_round"],
    },
    "cost": {
        "model_turns": {"supervisor": 2, "triage": 8, "deep": 10, "other": 4},
        "items": 4,
        "triage_items": 4,
        "deep_items": 2,
    },
    "review": {
        "admitted": ["SYN-C-1", "SYN-C-3"],
        "deferred": [],
        "dropped": [{"key": "SYN-C-2", "reason": "low"}, {"key": "SYN-C-4", "reason": "low"}],
        "audit": {"sampled": ["SYN-C-3"], "misses": ["SYN-C-3"], "miss_rate": "1 of 1"},
    },
    "goal": {
        "turns": [
            {"seq": 5, "goal_ok": False, "clears": "supervisor", "goal": "4 items: x"},
            {"seq": 9, "goal_ok": False, "clears": "supervisor", "goal": "re-open drops"},
        ],
        "evaluations": [{"seq": 12, "ok": True, "code": ""}],
        "done_seq": 12,
        "table_entered_seq": 13,
    },
}


def ctx(outputs: dict[str, Any], key: dict[str, Any] | None = KEY) -> Any:
    def get(kind: str, name: str) -> Any:
        found = outputs.get(name) if kind == "portfolio" else None
        return None if found is None else SimpleNamespace(content=found)

    def resolve(ref: str) -> Any:
        if key is None:
            raise KeyRefMissing(message="no section 'portfolio'")
        return key

    return SimpleNamespace(state=SimpleNamespace(get=get), key=SimpleNamespace(resolve=resolve))


def mutate(path: str, value: Any) -> dict[str, Any]:
    out = copy.deepcopy(ORACLE)
    *head, last = path.split(".")
    node = out
    for part in head:
        node = node[part]
    node[last] = value
    return out


CHECKS: dict[str, tuple[Any, list[tuple[str, Any]]]] = {
    "board_only": (p.board_only, [("supervisor.rendered", ["patent.extraction/SYN-C-1@v1"]),
                                  ("supervisor.tool_calls", {"commit_round": 1, "read_note": 1}),
                                  ("supervisor.max_context_chars", 90_000)]),
    "tiers": (p.tiers_save_calls, [("cost.model_turns", {"supervisor": 20, "triage": 8,
                                                          "deep": 10, "other": 4}),
                                   ("cost.deep_items", 4)]),
    "audit": (p.audit_catches_planted_drop, [("review.audit.sampled", []),
                                             ("review.admitted", ["SYN-C-1"]),
                                             ("review.audit.miss_rate", None)]),
    "goal": (p.goal_holds, [("goal.turns", []), ("goal.done_seq", None),
                            ("goal.table_entered_seq", 3),
                            ("review.dropped", [{"key": "SYN-C-2", "reason": "low"}])]),
}  # fmt: skip


@pytest.mark.parametrize("name", list(CHECKS))
def test_each_check_passes_the_oracle(name: str) -> None:
    check, _ = CHECKS[name]
    assert check(None, ctx(ORACLE), None).outcome == "pass"


@pytest.mark.parametrize(
    ("name", "path", "value"),
    [(n, path, v) for n, (_, mutants) in CHECKS.items() for path, v in mutants],
)
def test_each_mutant_is_killed(name: str, path: str, value: Any) -> None:
    check, _ = CHECKS[name]
    assert check(None, ctx(mutate(path, value)), None).outcome == "fail"


def test_an_unsupervised_run_fails_every_check() -> None:
    for check, _ in CHECKS.values():
        assert check(None, ctx({}), None).outcome == "fail"


def test_a_world_without_a_portfolio_key_is_ungradeable_not_a_pass() -> None:
    from aksha.evals.criteria import REGISTRY

    for cid in ("portfolio.board_only", "portfolio.goal_holds"):
        result = REGISTRY[cid].grade(None, ctx(ORACLE, key=None), None)  # type: ignore[arg-type]
        assert (result.outcome, result.cause) == ("ungradeable", "key_ref_missing")
