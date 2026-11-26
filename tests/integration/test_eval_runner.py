"""The eval runner end to end: suites through the scripted agent, results as events, the CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aksha.app import eval_agents, eval_judge
from aksha.evals.agents.scripted import ScriptedResearchAgent
from aksha.evals.case import load_suite
from aksha.evals.errors import EvalError
from aksha.evals.events import results_catalogue
from aksha.evals.registry import AgentRegistry
from aksha.evals.report import verdict
from aksha.evals.results import RESULTS_FILE, SuiteResults, project_results
from aksha.evals.runner import SuiteRun, rebuild_report, run_suite
from aksha.interfaces.cli.main import main
from tests.support.evals import FIXTURES, OPEN, REPO, SUITES


@pytest.fixture(scope="module")
def full_run(tmp_path_factory: pytest.TempPathFactory) -> SuiteRun:
    return run_suite(
        load_suite(SUITES / "scripted"),
        fixtures_root=FIXTURES,
        agents=eval_agents(),
        open_store=OPEN,
        out=tmp_path_factory.mktemp("run"),
        judge=eval_judge(),
        conditions={"runtime_commit": "test", "dirty": True},
    )


def test_the_scripted_suite_passes_with_self_tests_first(full_run: SuiteRun) -> None:
    text = full_run.reports["report.txt"].read_text()
    lines = text.splitlines()
    assert lines[0].startswith("self-tests: pass; mutation coverage")
    assert lines[1] == "fake model mode: rates are plumbing checks, not reliability"
    assert "1 world cluster(s): counts only" in text
    assert "trap handling" not in text  # never a trap tally in fake mode
    assert full_run.exit_code == 0


def test_results_are_events_and_the_report_is_their_projection(full_run: SuiteRun) -> None:
    store = OPEN(full_run.out / RESULTS_FILE, results_catalogue())
    try:
        assert store.verify() is None
        types = [e.type for e in store.read("eval:scripted")]
        assert types[0] == "eval.suite_started" and types[-1] == "eval.suite_ended"
        assert types.count("eval.episode_graded") == len(full_run.records)
        assert "eval.relation_checked" in types and "eval.selftest_recorded" in types
        projected = project_results(store, "scripted")
    finally:
        store.close()
    assert projected == full_run.results
    rows = [json.loads(line) for line in (full_run.out / "results.jsonl").read_text().splitlines()]
    assert len(rows) == len(full_run.records)
    assert all(r["conditions"]["world_lock"].startswith("sha256:") for r in rows)
    assert all(r["conditions"]["agent_definition_hash"].startswith("sha256:") for r in rows)
    assert all(r["trace_path"].endswith("trace.jsonl") for r in rows)


def test_rebuilding_the_report_from_the_store_gives_the_same_text(full_run: SuiteRun) -> None:
    before = full_run.reports["report.txt"].read_text()
    results, code = rebuild_report(OPEN, full_run.out, "scripted")
    assert code == 0 and results == full_run.results
    assert full_run.reports["report.txt"].read_text() == before


def test_seeded_faults_make_rollouts_differ_and_report_pass_hat_k(full_run: SuiteRun) -> None:
    flaky = [ep for ep in full_run.results.episodes if ep["case"] == "r-flaky-pages"]
    assert len(flaky) == 5
    assert len({ep["trace_hash"] for ep in flaky}) > 1
    report = json.loads(full_run.reports["report.json"].read_text())
    row = next(r for r in report["cases"] if r["case"] == "r-flaky-pages")
    assert row["k_eff"] > 1 and row["pass_hat_k"] is not None
    plain = next(r for r in report["cases"] if r["case"] == "r-prices")
    assert plain["k_eff"] == 1 and plain["pass_hat_k"] is None


def test_invariance_runs_compare_base_and_renamed_variant(full_run: SuiteRun) -> None:
    rels = [r for r in full_run.results.relations if r["case"] == "r-duplicate-competitor"]
    assert rels and rels[0]["base"] == "stillroom" and rels[0]["variant"] == "stillroom-v2"
    ids = {c["id"]: c["outcome"] for c in rels[0]["criteria"]}
    assert ids == {"rel.invariant": "pass", "rel.same_verdict": "pass", "rel.same_calls": "pass"}


def test_the_judge_annotates_without_gating(full_run: SuiteRun) -> None:
    ep = next(e for e in full_run.results.episodes if e["case"] == "r-prices")
    judged = [c for c in ep["criteria"] if c["layer"] == "judge"]
    assert judged and judged[0]["must"] is False
    assert "uncalibrated, annotates" in judged[0]["evidence"][0]


def test_two_runs_write_the_same_traces(full_run: SuiteRun, tmp_path: Path) -> None:
    again = run_suite(
        load_suite(SUITES / "scripted"),
        fixtures_root=FIXTURES,
        agents=eval_agents(),
        open_store=OPEN,
        out=tmp_path,
        selftests=False,
    )
    first = {k: r.trace_hash for k, r in full_run.records.items()}
    second = {k: r.trace_hash for k, r in again.records.items()}
    assert first == second


class _NameBranching(ScriptedResearchAgent):
    """Behaves differently when it recognises the renamed world: invariance must catch it."""

    def run(self, episode):  # type: ignore[no-untyped-def]
        super().run(episode)
        if episode.world == "stillroom-v2":
            episode.save("note", "extra", {"text": "only in the variant"})


def test_an_agent_that_branches_on_the_world_fails_invariance(tmp_path: Path) -> None:
    agents = AgentRegistry()
    agents.register("scripted", _NameBranching)
    run = run_suite(
        load_suite(SUITES / "scripted"),
        fixtures_root=FIXTURES,
        agents=agents,
        open_store=OPEN,
        out=tmp_path,
        selftests=False,
        cases=["r-duplicate-competitor"],
    )
    (rel,) = run.results.relations
    inv = next(c for c in rel["criteria"] if c["id"] == "rel.invariant")
    assert inv["outcome"] == "fail"
    assert run.exit_code == 1


def test_a_failing_agent_fails_the_suite_and_names_the_criterion(tmp_path: Path) -> None:
    class _Splitter(ScriptedResearchAgent):
        def _resolve(self, pages):  # type: ignore[no-untyped-def]
            return [[p] for p in pages]

    agents = AgentRegistry()
    agents.register("scripted", _Splitter)
    run = run_suite(
        load_suite(SUITES / "scripted"),
        fixtures_root=FIXTURES,
        agents=agents,
        open_store=OPEN,
        out=tmp_path,
        selftests=False,
        cases=["r-duplicate-competitor"],
    )
    assert run.exit_code == 1
    v = run.verdicts[("r-duplicate-competitor", "stillroom", 0)]
    assert v.first_failure is not None and v.first_failure.id == "research.competitor_count"
    trap = v.result("trap.T-DUP")
    assert trap is not None and trap.reading == "fell_for_it"
    assert "failure clusters: " in run.reports["report.txt"].read_text()


def test_an_unregistered_agent_is_refused(tmp_path: Path) -> None:
    with pytest.raises(EvalError) as e:
        run_suite(
            load_suite(SUITES / "research"),
            fixtures_root=FIXTURES,
            agents=AgentRegistry(),
            open_store=OPEN,
            out=tmp_path,
        )
    assert e.value.code == "agent_not_registered"


def test_the_research_suite_runs_with_an_agent_override(tmp_path: Path) -> None:
    run = run_suite(
        load_suite(SUITES / "research"),
        fixtures_root=FIXTURES,
        agents=eval_agents(),
        open_store=OPEN,
        out=tmp_path,
        selftests=False,
        cases=["r-duplicate-competitor", "r-prices"],
        agent_override="scripted",
    )
    assert run.exit_code == 0


def test_xfail_is_strict_in_the_suite_verdict() -> None:
    def ep(v: str, xfail: bool) -> dict[str, object]:
        return {"verdict": v, "xfail": xfail, "criteria": []}

    assert verdict(SuiteResults(episodes=(ep("fail", True),))) == "pass"
    assert verdict(SuiteResults(episodes=(ep("pass", True),))) == "fail"
    assert (
        verdict(SuiteResults(episodes=(ep("ungradeable", False), ep("fail", False))))
        == "ungradeable"
    )
    assert (
        verdict(SuiteResults(selftests=({"ok": False},), episodes=(ep("pass", False),)))
        == "ungradeable"
    )


# --- the command line ------------------------------------------------------------------


def test_cli_runs_a_suite_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "eval",
            "--root",
            str(REPO),
            "run",
            "scripted",
            "--no-selftest",
            "--case",
            "r-prices",
            "--out",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert out.startswith("self-tests: not run")
    assert main(["eval", "--root", str(REPO), "report", str(tmp_path), "--suite", "scripted"]) == 0


def test_cli_key_check_list_and_refusals(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval", "--root", str(REPO), "key-check"]) == 0
    assert "stillroom: 0 drifts" in capsys.readouterr().out
    assert main(["eval", "--root", str(REPO), "list"]) == 0
    assert main(["--user", "o1", "eval", "--root", str(REPO), "list"]) == 0
    assert "agents: patent, research, scripted" in capsys.readouterr().out
    assert main(["eval", "--root", str(REPO), "run", "no-such-suite"]) == 65
    assert main(["eval", "frobnicate"]) == 64


def test_cli_selftest_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["eval", "--root", str(REPO), "selftest", "--world", "stillroom", "--out", str(tmp_path)]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    assert "stillroom: self-tests pass; mutation coverage" in out
