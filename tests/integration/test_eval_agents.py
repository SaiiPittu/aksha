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
    # The agent profiles companies in the order it discovered their pages, not by name, so
    # a rename leaves its reads in the same order (rel.same_calls). It finds its inputs by
    # content, so it also opens the file the variant adds (family E's publication); that
    # read is in the changed set and left out of the comparison.
    assert outcomes == {
        "rel.invariant": "pass",
        "rel.same_verdict": "pass",
        "rel.same_calls": "pass",
    }


def test_the_research_agent_reads_only_through_the_episode(research: SuiteRun) -> None:
    for rec in research.records.values():
        calls = [e for e in rec.events if e["type"] == "episode.tool_called"]
        tools = {c["payload"]["tool"] for c in calls}
        assert calls and tools <= {"list_sources", "read_source", "web_search", "read_web"}
        assert {"web_search", "read_web"} <= tools  # the world's web, through the episode too
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


def test_the_episode_mailbox_never_sends_a_landed_key_twice(patent: SuiteRun) -> None:
    """A send that landed before a restart is found in the episode's store, not sent again."""
    from aksha.app import eval_store_opener
    from aksha.evals.agents.runtime import EpisodeMailbox
    from aksha.evals.episode import Episode
    from aksha.evals.events import episode_catalogue

    (case, _world, _rollout), rec = next(iter(patent.records.items()))
    assert rec.path is not None
    (row,) = rec.ledger
    folder = rec.path.parent if rec.path.name == "bundle" else rec.path
    store = eval_store_opener()(folder / "store.sqlite", episode_catalogue())
    try:
        episode = Episode(store=store, owner=f"episode:{case}", root=folder / "world",
                          case_id="c", world="w", rollout=0, seed=0)  # fmt: skip
        box = EpisodeMailbox("counsel_mail", episode)  # a fresh process: no grants, no memory
        receipt = box.apply(str(row["effect_key"]), {})
        assert receipt.body["effect_key"] == row["effect_key"]
        assert episode.receiver.rows == []
    finally:
        store.close()


@pytest.fixture(scope="module")
def general(tmp_path_factory: pytest.TempPathFactory) -> SuiteRun:
    return _run(tmp_path_factory.mktemp("tilldew"), "tilldew")


def test_both_agents_pass_a_world_their_code_has_never_seen(general: SuiteRun) -> None:
    """The tilldew suite: another domain, layout, column names, headings and form fields.

    The agents run with the same code as on stillroom; their offline models follow general
    rules here (the stillroom lexicon applies only to the stillroom notes).
    """
    verdicts = _verdicts(general)
    assert len(verdicts) == 12 and set(verdicts.values()) == {"pass"}, verdicts
    research = [c for c in verdicts if c.startswith("g-research")]
    patent = [c for c in verdicts if c.startswith("g-patent")]
    assert len(research) == 6 and len(patent) == 6
    sends = [rec for rec in general.records.values() if rec.ledger]
    assert sends and all(len(rec.ledger) == 1 for rec in sends)  # each packet sent once


@pytest.fixture(scope="module")
def other_shapes(tmp_path_factory: pytest.TempPathFactory) -> SuiteRun:
    return _run(tmp_path_factory.mktemp("cindral"), "cindral")


def test_both_agents_pass_a_world_of_other_shapes(other_shapes: SuiteRun) -> None:
    """The cindral suite: a third domain whose inputs differ in structure, not only in names.

    Competitors as PDF text and a pasted tab-separated spreadsheet (a seller found only
    there), reviews as a nested JSON export, inventions as meeting notes and a slide-deck
    export, contributors as nested JSON with no person-ID column, disclosures as a table
    inside a note, priorities as a CSV, one firm's own form, publications as labelled text
    and a JSON export with other keys. Each record is decided by kind, then extracted by
    that kind's schema, through the same code that runs the other worlds.
    """
    verdicts = _verdicts(other_shapes)
    assert len(verdicts) == 11 and set(verdicts.values()) == {"pass"}, verdicts
    sends = [rec for rec in other_shapes.records.values() if rec.ledger]
    assert sends and all(len(rec.ledger) == 1 for rec in sends)
    assert {rec.ledger[0]["target_ref"] for rec in sends} == {"packet/INV-7"}
