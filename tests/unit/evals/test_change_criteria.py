"""The change checks (``change.*``, ``research.scope_change``) against the change set
``scope-eu-uk``'s derived key: each passes the outputs a correct agent leaves and fails the
mutant that breaks it. A change case is graded with its change set's key, as the runner does."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aksha.evals.agents.scripted import ScriptedAgent
from aksha.evals.case import ChangeStep
from aksha.evals.criteria import REGISTRY
from aksha.evals.grade import grade
from aksha.evals.runner import grading_key
from tests.support.evals import case, key, run_episode, world

CRITERIA = ("change.stale_set", "change.unchanged_set", "research.scope_change")
THEMES_EU = {"battery": 2, "stillness": 4, "setup": 5, "privacy": 2}
THEMES_EU_UK = {"battery": 5, "stillness": 5, "setup": 5, "privacy": 2}
STEP = ChangeStep("episode.output_saved:scope/all", "scope-eu-uk", restart=True)


def _scope(regions: list[str]) -> tuple[Any, ...]:
    return ("save", "scope", "all", {"regions": regions}, [])


def _summary(themes: dict[str, int]) -> tuple[Any, ...]:
    return ("save", "review_summary", "all", {"themes_in_scope": dict(themes)}, [])


COMPETITOR: tuple[Any, ...] = ("save", "competitor", "E1", {"name": "Kelvra Systems"}, [])
BEFORE = [COMPETITOR, _summary(THEMES_EU), _scope(["EU"])]
CORRECT = [*BEFORE, _summary(THEMES_EU_UK), _scope(["EU", "UK"])]


def _outcomes(
    tmp_path: Path, steps: list[tuple[Any, ...]], *, change: bool = True
) -> dict[str, str]:
    c = case(agents=("research",), category="change", criteria=CRITERIA,
             change_steps=(STEP,) if change else ())  # fmt: skip
    rec = run_episode(ScriptedAgent(steps), tmp_path, c=c)
    w = world()
    v = grade(rec, c, grading_key(w, key(), rec), w, REGISTRY)
    return {r.id: r.outcome for r in v.results if r.id in CRITERIA}


def test_the_correct_outputs_pass_every_change_check(tmp_path: Path) -> None:
    assert _outcomes(tmp_path, CORRECT) == dict.fromkeys(CRITERIA, "pass")


@pytest.mark.parametrize(
    ("steps", "fails"),
    [
        # the scope never moves: stale and the amended values both fail
        (BEFORE, {"change.stale_set", "research.scope_change"}),
        # the summary is not redone: one stale kind missed
        ([*BEFORE, _scope(["EU", "UK"])], {"change.stale_set", "research.scope_change"}),
        # the competitors are rebuilt too: the early cutoff did not hold
        (
            [*CORRECT, ("save", "competitor", "E1", {"name": "Kelvra"}, [])],
            {"change.unchanged_set"},
        ),
        # the scope moved but to the wrong regions
        ([*BEFORE, _summary(THEMES_EU_UK), _scope(["UK"])], {"research.scope_change"}),
    ],
    ids=["nothing-rebuilt", "summary-stale", "competitor-rebuilt", "wrong-regions"],
)
def test_each_mutant_fails_the_check_it_breaks(
    tmp_path: Path, steps: list[tuple[Any, ...]], fails: set[str]
) -> None:
    got = _outcomes(tmp_path, steps)
    assert {c for c, o in got.items() if o == "fail"} == fails


def test_without_an_applied_change_the_change_checks_fail(tmp_path: Path) -> None:
    """Graded with the derived key but with no change applied, nothing is rewarded."""
    c = case(agents=("research",), category="change", criteria=CRITERIA)
    rec = run_episode(ScriptedAgent(CORRECT), tmp_path, c=c)
    w = world()
    derived = grading_key(w, key(), rec)
    assert "change" not in derived.data  # no change applied: the world's own key
    from aksha.evals.selftest.run import case_key

    with_steps = case(agents=("research",), category="change", criteria=CRITERIA,
                      change_steps=(STEP,))  # fmt: skip
    v = grade(rec, with_steps, case_key(w, key(), with_steps), w, REGISTRY)
    got = {r.id: r.outcome for r in v.results}
    assert got["change.stale_set"] == got["change.unchanged_set"] == "fail"


def test_a_world_key_without_a_change_section_is_a_key_gap(tmp_path: Path) -> None:
    got = _outcomes(tmp_path, BEFORE, change=False)
    assert got == dict.fromkeys(CRITERIA, "ungradeable")
