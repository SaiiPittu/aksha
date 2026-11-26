import math
from datetime import date

import pytest

from aksha.evals.judge import (
    HandLabel,
    Judge,
    RuleJudge,
    calibrate,
    cohen_kappa,
    corrected_rate,
    may_gate,
)
from aksha.evals.metrics import (
    clustered_se,
    cost_per_pass,
    failure_clusters,
    k_effective,
    pass_at_k,
    pass_hat_k,
    wilson,
)
from aksha.runtime.models.types import ClassificationRequest, ClassificationResult


def test_pass_at_k_and_pass_hat_k_match_the_worked_example() -> None:
    # Eval design section 5: n = 5 rollouts, 4 pass.
    assert pass_at_k(4, 5, 1) == pytest.approx(0.8)
    assert pass_hat_k(4, 5, 3) == pytest.approx(0.4)
    assert pass_hat_k(4, 5, 3) < 0.8**3  # the naive figure flatters it
    assert pass_at_k(4, 5, 3) == 1.0
    assert math.isnan(pass_at_k(3, 3, 4)) and math.isnan(pass_hat_k(1, 2, 3))
    # ADR-P22's toy: 3 of 4 pass.
    assert pass_hat_k(3, 4, 2) == pytest.approx(0.5)
    assert pass_at_k(3, 4, 2) == 1.0


def test_k_effective_counts_distinct_traces() -> None:
    assert k_effective(["a", "a", "a"]) == 1
    assert k_effective(["a", "b", "a"]) == 2


def test_wilson_interval_matches_the_design() -> None:
    lo, hi = wilson(4, 5)
    assert (round(lo, 2), round(hi, 2)) == (0.38, 0.96)
    lo, hi = wilson(16, 20)
    assert (round(lo, 2), round(hi, 2)) == (0.58, 0.92)
    assert wilson(0, 0) == (0.0, 1.0)


def test_cost_per_pass_charges_failures_to_the_passes() -> None:
    cost = cost_per_pass([52, 48, 61, 50, 140], [True, True, True, True, False])
    assert cost.per_pass == pytest.approx(87.75)
    assert (cost.median, cost.maximum) == (52, 140)
    assert math.isinf(cost_per_pass([3], [False]).per_pass)


def test_clustered_errors_need_two_clusters() -> None:
    assert clustered_se([1.0, 0.0, 1.0], ["w", "w", "w"]) == (None, 1)
    se, g = clustered_se([1.0, 0.0, 1.0, 1.0], ["a", "a", "b", "b"])
    assert g == 2 and se is not None and se > 0


def test_failure_clusters_group_by_signature() -> None:
    fails = [("trap.T-DUP", "trap", "6 records: x", f"c/w/r{i}") for i in range(4)]
    fails.append(("research.prices", "state", "KF-17: K1 lacks", "c/w/r0"))
    clusters = failure_clusters(fails)
    assert [c.count for c in clusters] == [4, 1]
    assert clusters[0].examples == ("c/w/r0", "c/w/r1", "c/w/r2")


# --- the judge -------------------------------------------------------------------------


def _labels() -> list[HandLabel]:
    # Eval design 3.4 toy: 20 real passes (judge passes 18), 20 real failures (judge fails 15).
    passes = [HandLabel(f"p{i}", True, i < 18) for i in range(20)]
    fails = [HandLabel(f"f{i}", False, i >= 15) for i in range(20)]
    return passes + fails


def test_calibration_matches_the_worked_example_and_does_not_gate() -> None:
    cal = calibrate("judge.x", "judge-m", "1", _labels(), [True] * 5, date(2026, 11, 26))
    assert (cal.tpr_ppm, cal.tnr_ppm) == (900_000, 750_000)
    assert cal.kappa_ppm == 650_000
    assert round(cal.tnr_lower_ppm / 1e6, 2) == 0.53
    assert not may_gate(cal)  # kappa clears 0.6 but the TNR lower bound misses 0.7


def test_a_judge_gates_only_with_enough_real_failures_and_every_known_bad_caught() -> None:
    good = [HandLabel(f"p{i}", True, True) for i in range(40)] + [
        HandLabel(f"f{i}", False, False) for i in range(40)
    ]
    cal = calibrate("judge.x", "m", "1", good, [True, True], date(2026, 11, 26))
    assert may_gate(cal)
    missed = calibrate("judge.x", "m", "1", good, [True, False], date(2026, 11, 26))
    assert not may_gate(missed)
    few = calibrate("judge.x", "m", "1", good[:40] + good[40:60], [True], date(2026, 11, 26))
    assert not may_gate(few)


def test_kappa_edges() -> None:
    assert cohen_kappa([(True, True), (False, False)]) == 1.0
    assert cohen_kappa([(True, True), (True, True)]) == 1.0
    assert cohen_kappa([]) == 0.0


def test_the_corrected_rate_is_clipped_with_a_bootstrap_interval() -> None:
    result = corrected_rate(0.8, _labels(), resamples=2000, seed=1)
    assert result is not None
    point, lo, hi = result
    assert point == pytest.approx(0.55 / 0.65)
    assert lo < point <= hi <= 1.0
    useless = [HandLabel("p", True, False), HandLabel("f", False, True)]
    assert corrected_rate(0.5, useless) is None


class _Broken:
    model_class = "classification"
    capabilities = RuleJudge.capabilities

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        raise TimeoutError("judge down")


def test_the_fake_judge_reads_only_its_state() -> None:
    from aksha.runtime.models.types import QuestionSpec

    q = QuestionSpec(
        name="judge.unknown_price_explained",
        prompt="?",
        type="choice",
        options=("FAIL", "PASS", "UNKNOWN"),
    )
    judge = RuleJudge()

    def ask(state: str) -> object:
        answer = judge.classify(ClassificationRequest("s", "a", state, (q,))).answer(q.name)
        return answer.choice if answer else "missing"

    assert ask("output:\nprice: None label: unknown\nkeyed passages:\n- x") == "PASS"
    assert ask("output:\nprice: 0 label: stated contact sales") == "FAIL"
    assert ask("output:\nnothing relevant\nkeyed passages:\n- Contact sales for a quote.") is None


def test_a_judge_outage_is_ungradeable_not_a_failure() -> None:
    import tempfile
    from pathlib import Path

    from aksha.evals.agents.scripted import ScriptedAgent
    from aksha.evals.grade import GradeContext
    from aksha.evals.state import fold
    from tests.support.evals import case, key, run_episode, world

    with tempfile.TemporaryDirectory() as d:
        rec = run_episode(ScriptedAgent([]), Path(d))
        ctx = GradeContext(key=key(), world=world(), state=fold(rec.events), rec=rec)
        c = case(judge=("judge.unknown_price_explained",))
        (down,) = Judge(_Broken(), "broken")(rec, ctx, c)  # type: ignore[arg-type]
        assert (down.outcome, down.cause, down.must) == ("ungradeable", "judge_unavailable", False)
        (unknown,) = Judge(RuleJudge(), "fake")(rec, ctx, c)
        assert (unknown.outcome, unknown.cause) == ("ungradeable", "judge_unknown")
