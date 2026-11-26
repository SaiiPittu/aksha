from dataclasses import replace
from pathlib import Path

import pytest

from aksha.evals.agents.scripted import ScriptedAgent, ScriptedResearchAgent
from aksha.evals.bundle import EpisodeRecord
from aksha.evals.criteria import REGISTRY
from aksha.evals.errors import CaseInvalid
from aksha.evals.grade import (
    UNGRADEABLE_CAUSES,
    Criterion,
    failed,
    grade,
    order,
    passed,
    ungradeable,
)
from aksha.evals.judge import Judge, RuleJudge
from tests.support.evals import case, graded, key, run_episode, world


@pytest.fixture(scope="module")
def research(tmp_path_factory: pytest.TempPathFactory) -> EpisodeRecord:
    return run_episode(ScriptedResearchAgent(), tmp_path_factory.mktemp("research"))


def test_the_cascade_runs_layers_in_order_and_names_the_first_failure() -> None:
    ids = ["trap.T-DUP", "citation.exists", "research.prices", "inv.one_row_per_key"]
    assert order(ids, REGISTRY) == [
        "inv.one_row_per_key",
        "research.prices",
        "citation.exists",
        "trap.T-DUP",
    ]
    with pytest.raises(CaseInvalid):
        order(["nope"], REGISTRY)


def test_a_passing_episode_passes_and_should_results_never_fail_it(research: EpisodeRecord) -> None:
    c = case(
        criteria=("research.competitor_count", "trap.T-DUP"),
        should=("research.theme_counts", "patent.matrix"),
    )
    v = graded(research, c)
    assert v.outcome == "pass"
    assert v.result("patent.matrix") is not None
    assert v.result("patent.matrix").outcome == "fail"  # type: ignore[union-attr]
    assert v.must_passed == (2, 2)


def test_a_failed_must_fails_the_episode(research: EpisodeRecord) -> None:
    v = graded(research, case(criteria=("research.competitor_count", "patent.matrix")))
    assert v.outcome == "fail"
    assert v.first_failure is not None and v.first_failure.id == "patent.matrix"


def test_a_missing_key_field_is_ungradeable_never_a_fail(research: EpisodeRecord) -> None:
    v = graded(research, case(criteria=("research.competitor_count", "precision.abstention_count")))
    r = v.result("precision.abstention_count")
    assert r is not None and (r.outcome, r.cause) == ("ungradeable", "key_ref_missing")
    assert (v.outcome, v.cause) == ("ungradeable", "key_ref_missing")


def test_a_fail_outranks_an_ungradeable(research: EpisodeRecord) -> None:
    v = graded(research, case(criteria=("patent.matrix", "precision.abstention_count")))
    assert v.outcome == "fail"


def test_a_grader_exception_is_a_grader_error(research: EpisodeRecord) -> None:
    def boom(*_: object) -> object:
        raise ZeroDivisionError("bad grader")

    registry = {**REGISTRY, "broken": Criterion("broken", "state", boom)}  # type: ignore[arg-type]
    v = grade(research, case(criteria=("broken",)), key(), world(), registry)
    r = v.result("broken")
    assert r is not None and r.cause == "grader_error" and "ZeroDivisionError" in r.evidence[0]


def test_the_gate_refuses_a_broken_chain_and_a_changed_world(research: EpisodeRecord) -> None:
    c = case(criteria=("research.competitor_count",))
    broken = research.edited(meta={"store_verified": False, "store_break": "seq 4"})
    assert (graded(broken, c).outcome, graded(broken, c).cause) == (
        "ungradeable",
        "store_fold_failed",
    )
    moved = research.edited(meta={"world_lock_problems": ["sources/notes/C1.md"]})
    assert graded(moved, c).cause == "world_lock_mismatch"
    shuffled = replace(research, events=tuple(dict(e, seq=1) for e in research.events))
    assert graded(shuffled, c).cause == "store_fold_failed"
    garbled = replace(research, events=({"seq": 1, "type": "episode.output_saved", "payload": {}},))
    assert graded(garbled, c).cause == "store_fold_failed"


def test_check_helpers_keep_outcomes_closed() -> None:
    assert passed("x").outcome == "pass"
    assert failed("x", reading="missed").reading == "missed"
    for cause in UNGRADEABLE_CAUSES:
        assert ungradeable(cause).cause == cause
    with pytest.raises(ValueError):
        ungradeable("felt_wrong")


def test_the_judge_runs_only_after_every_must_passed(
    tmp_path: Path, research: EpisodeRecord
) -> None:
    judge = Judge(RuleJudge(), "fake:RuleJudge")
    ok = grade(
        research,
        case(criteria=("research.prices",), judge=("judge.unknown_price_explained",)),
        key(),
        world(),
        REGISTRY,
        judge=judge,
    )
    j = ok.result("judge.unknown_price_explained")
    assert j is not None and j.outcome == "pass" and j.must is False
    bad = grade(
        research,
        case(criteria=("patent.matrix",), judge=("judge.unknown_price_explained",)),
        key(),
        world(),
        REGISTRY,
        judge=judge,
    )
    assert bad.result("judge.unknown_price_explained") is None


def test_an_empty_episode_fails_and_keeps_invariants(tmp_path: Path) -> None:
    rec = run_episode(ScriptedAgent([]), tmp_path)
    v = graded(
        rec, case(criteria=("inv.one_row_per_key", "no_unbound_send", "research.competitor_count"))
    )
    assert v.outcome == "fail"
    assert v.result("inv.one_row_per_key").outcome == "pass"  # type: ignore[union-attr]
    assert v.result("no_unbound_send").outcome == "pass"  # type: ignore[union-attr]
