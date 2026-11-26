"""Resume against fresh, runnable (decision 36): the scope-change case through its arms.

The research agent runs as it ships, the founder's scope amendment lands after a finished
request run, the runtime records the source change and drives the rebuilds, and each arm is
graded against the change set's derived key and measured from the runtime store. In fake
model mode the measures are a property of the rule-based fake; these tests prove the arms
run under their own settings, record their re-entry choices and produce the numbers, not
that one mode is better.
"""

from __future__ import annotations

import json

import pytest

from aksha.app import eval_agents, eval_judge
from aksha.evals.case import load_suite
from aksha.evals.runner import SuiteRun, run_suite
from tests.support.evals import FIXTURES, OPEN, SUITES

CASE = "change-scope-resume-vs-fresh"


@pytest.fixture(scope="module")
def change(tmp_path_factory: pytest.TempPathFactory) -> SuiteRun:
    return run_suite(
        load_suite(SUITES / "change"),
        fixtures_root=FIXTURES,
        agents=eval_agents(),
        open_store=OPEN,
        out=tmp_path_factory.mktemp("change"),
        judge=eval_judge(),
        selftests=False,
        conditions={"runtime_commit": "test", "dirty": True},
    )


def _rows(run: SuiteRun) -> dict[str, dict[str, object]]:
    rows = [json.loads(line) for line in (run.out / "results.jsonl").read_text().splitlines()]
    return {str(r["arm"]["name"]): r for r in rows}


def test_every_arm_rebuilds_the_amended_scope_correctly(change: SuiteRun) -> None:
    assert {k: v.outcome for k, v in change.verdicts.items()} == {
        (f"{CASE}[fresh]", "stillroom", 0): "pass",
        (f"{CASE}[resume]", "stillroom", 0): "pass",
        (f"{CASE}[resume_scope_exempt]", "stillroom", 0): "pass",
    }
    for v in change.verdicts.values():
        got = {r.id: r.outcome for r in v.results}
        assert got["change.stale_set"] == got["change.unchanged_set"] == "pass"
        assert got["research.scope_change"] == "pass"


def test_each_arm_records_its_own_setting_and_reentry_choices(change: SuiteRun) -> None:
    rows = _rows(change)
    fresh = rows["fresh"]["metrics"]["rebuild"]  # type: ignore[index]
    assert fresh["reentry_modes"] == {"fresh": fresh["reentry_modes"]["fresh"]}
    assert set(fresh["fresh_reasons"]) == {"resume_disabled"}
    resume = rows["resume"]["metrics"]["rebuild"]  # type: ignore[index]
    assert "resume_disabled" not in resume["fresh_reasons"]
    assert "premise_changed" in resume["fresh_reasons"]  # the scope's direct readers
    exempt = rows["resume_scope_exempt"]["metrics"]["rebuild"]  # type: ignore[index]
    assert exempt["reentry_modes"].get("resume", 0) >= 1
    assert "premise_changed" not in exempt["fresh_reasons"]


def test_the_exempt_arm_is_another_definition(change: SuiteRun) -> None:
    rows = _rows(change)
    digest = {a: r["conditions"]["agent_definition_hash"] for a, r in rows.items()}  # type: ignore[index]
    assert digest["fresh"] == digest["resume"] != digest["resume_scope_exempt"]


def test_the_report_has_one_row_per_arm_and_the_comparison(change: SuiteRun) -> None:
    text = (change.out / "report.txt").read_text()
    for arm in ("fresh", "resume", "resume_scope_exempt"):
        assert f"{CASE}[{arm}] |" in text
    assert "resume against fresh:" in text and "resume_scope_exempt against fresh:" in text
    assert "properties of the rule-based fake" in text
    arms = json.loads((change.out / "report.json").read_text())["arms"]
    assert [a["arm"] for a in arms] == ["fresh", "resume", "resume_scope_exempt"]


def test_a_rebuild_run_carries_the_request_it_rebuilds_for(change: SuiteRun) -> None:
    """No message opens a rebuild run; every pass of it shows the model the request of the
    run it rebuilds for (``run.started{for_runs}``), not an empty one."""
    from aksha.adapters.clock.fake import FakeClock
    from aksha.adapters.storage import sqlite
    from aksha.app import EVAL_CLOCK_START
    from aksha.runtime.projections.view import load_view
    from aksha.runtime.session.service import _request

    case = load_suite(SUITES / "change").case(CASE)
    rec = change.records[(f"{CASE}[fresh]", "stillroom", 0)]
    assert rec.path is not None
    db = rec.path.parent / "runtime" / "aksha.sqlite"
    store = sqlite.open_store(db, FakeClock(EVAL_CLOCK_START), writer=False)
    try:
        view = load_view(store, "evals")
        rebuilds = [e.run for e in view.events
                    if e.type == "run.started" and e.payload.get("cause") == "rebuild"]  # fmt: skip
        assert rebuilds and all(_request(view, str(r)) == case.opening for r in rebuilds)
    finally:
        store.close()
