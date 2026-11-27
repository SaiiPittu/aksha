"""The portfolio suite through the eval runner: the patent agent's supervisor, measured.

The patent agent runs as it ships on the twenty-candidate ``coldwick`` world. Above the
threshold of eight candidates it runs them under the supervisor Task; the suite's four
checks read what the runtime recorded (``aksha.evals.criteria.portfolio``).
"""

from __future__ import annotations

import pytest

from aksha.app import eval_agents, eval_judge
from aksha.evals.case import load_suite
from aksha.evals.runner import SuiteRun, run_suite
from tests.support.evals import FIXTURES, OPEN, SUITES

CHECKS = (
    "portfolio.board_only",
    "portfolio.tiers_save_calls",
    "portfolio.audit_catches_planted_drop",
    "portfolio.goal_holds",
)


@pytest.fixture(scope="module")
def portfolio(tmp_path_factory: pytest.TempPathFactory) -> SuiteRun:
    return run_suite(
        load_suite(SUITES / "portfolio"),
        fixtures_root=FIXTURES,
        agents=eval_agents(),
        open_store=OPEN,
        out=tmp_path_factory.mktemp("portfolio"),
        judge=eval_judge(),
        selftests=False,
        conditions={"runtime_commit": "test", "dirty": True},
    )


def test_the_portfolio_case_passes_every_check(portfolio: SuiteRun) -> None:
    ((_, verdict),) = portfolio.verdicts.items()
    results = {r.id: r for r in verdict.results}
    for check in CHECKS:
        assert results[check].outcome == "pass", (check, results[check].evidence)
    assert verdict.outcome == "pass", [(r.id, r.evidence) for r in verdict.results
                                       if r.must and r.outcome != "pass"]  # fmt: skip


def test_the_measured_numbers_are_on_the_results(portfolio: SuiteRun) -> None:
    ((_, verdict),) = portfolio.verdicts.items()
    evidence = {r.id: " ".join(r.evidence) for r in verdict.results}
    assert "no artifact shown" in evidence["portfolio.board_only"]
    assert "saved" in evidence["portfolio.tiers_save_calls"]
    assert "CW-15 sampled" in evidence["portfolio.audit_catches_planted_drop"]
    assert "re-open drops" in evidence["portfolio.goal_holds"]
