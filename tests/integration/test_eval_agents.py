"""The research and patent agents through the eval runner, on the runtime with their fakes.

Each case runs the shipped agent (its loader-built definition, its rule-based model, its
own tools) with every read going through the episode, and is graded on what it saved into
the episode. In fake model mode these are plumbing checks: they prove the agents' outputs
flow from tool to store to grader, not that a model can do the work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aksha.app import eval_agents, eval_judge
from aksha.evals.case import load_suite
from aksha.evals.runner import SuiteRun, run_suite
from tests.support.evals import FIXTURES, OPEN, SUITES


def _run(out: Path, suite: str, *cases: str) -> SuiteRun:
    return run_suite(
        load_suite(SUITES / suite),
        fixtures_root=FIXTURES,
        agents=eval_agents(),
        open_store=OPEN,
        out=out,
        judge=eval_judge(),
        selftests=False,
        cases=list(cases),
        conditions={"runtime_commit": "test", "dirty": True},
    )


@pytest.fixture(scope="module")
def research(tmp_path_factory: pytest.TempPathFactory) -> SuiteRun:
    return _run(tmp_path_factory.mktemp("research"), "research", "s1-research-happy")


@pytest.fixture(scope="module")
def patent(tmp_path_factory: pytest.TempPathFactory) -> SuiteRun:
    return _run(tmp_path_factory.mktemp("patent"), "patent", "s2-patent-happy")


def _verdicts(run: SuiteRun) -> dict[str, str]:
    return {f"{c}/{w}": v.outcome for (c, w, _), v in run.verdicts.items()}


def test_the_research_happy_path_passes_in_both_worlds(research: SuiteRun) -> None:
    assert _verdicts(research) == {
        "s1-research-happy/stillroom": "pass",
        "s1-research-happy/stillroom-v2": "pass",
    }
    report = json.loads((research.out / "report.json").read_text())
    (pair,) = report["relations"]
    outcomes = {c["id"]: c["outcome"] for c in pair["criteria"]}
    assert outcomes["rel.invariant"] == "pass" and outcomes["rel.same_verdict"] == "pass"
    # rel.same_calls fails today: the agent profiles companies in name order, so a rename
    # reorders its reads. The runner reports it; the agent's ordering is the finding.
    assert outcomes["rel.same_calls"] == "fail"


def test_the_research_agent_reads_only_through_the_episode(research: SuiteRun) -> None:
    for rec in research.records.values():
        calls = [e for e in rec.events if e["type"] == "episode.tool_called"]
        assert calls and {c["payload"]["tool"] for c in calls} <= {"list_sources", "read_source"}
    rows = [json.loads(line) for line in (research.out / "results.jsonl").read_text().splitlines()]
    assert {r["conditions"]["agent_definition_basis"] for r in rows} == {"declared"}


def test_the_patent_happy_path_passes_and_sends_once_under_its_approval(patent: SuiteRun) -> None:
    assert _verdicts(patent) == {
        "s2-patent-happy/stillroom": "pass",
        "s2-patent-happy/stillroom-v2": "pass",
    }
    for rec in patent.records.values():
        assert len(rec.ledger) == 1
        (row,) = rec.ledger
        approvals = [e for e in rec.events if e["type"] == "episode.approval_requested"]
        assert [a["payload"]["approval_id"] for a in approvals] == [row["approval_id"]]
        assert row["target_ref"] == "packet/C1"


def test_the_runtime_store_sits_beside_the_world_copy(patent: SuiteRun) -> None:
    folders = {p.parent for p in patent.out.rglob("runtime/aksha.sqlite")}
    assert folders and all((f.parent / "world" / "sources").is_dir() for f in folders)
    assert not list(patent.out.rglob("sources/**/aksha.sqlite"))
    assert json.loads(next(patent.out.rglob("meta.json")).read_text())["agent"] == "patent"
