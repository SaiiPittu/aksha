from pathlib import Path

import pytest

from aksha.app import eval_agents
from aksha.evals.criteria import REGISTRY
from aksha.evals.selftest.mutants import MUTANTS
from aksha.evals.selftest.run import SelfTestReport, relation_self_test, self_test
from aksha.evals.world import renaming
from tests.support.evals import OPEN, key, world


@pytest.fixture(scope="module")
def reports(tmp_path_factory: pytest.TempPathFactory) -> dict[str, SelfTestReport]:
    out = tmp_path_factory.mktemp("selftest")
    return {
        name: self_test(world(name), key(name), open_store=OPEN, out=out, registry=REGISTRY)
        for name in ("stillroom", "stillroom-v2")
    }


@pytest.mark.parametrize("name", ["stillroom", "stillroom-v2"])
def test_every_self_test_bundle_holds(reports: dict[str, SelfTestReport], name: str) -> None:
    report = reports[name]
    assert [(c.bundle, c.problems) for c in report.checks if not c.ok] == []
    assert report.ok


@pytest.mark.parametrize("name", ["stillroom", "stillroom-v2"])
def test_every_gradeable_criterion_is_killed_by_a_mutant(
    reports: dict[str, SelfTestReport], name: str
) -> None:
    report = reports[name]
    killed, total = report.coverage
    # The change checks read a change set's derived key, never a world's own key, so they
    # are key gaps here; tests/unit/evals/test_change_criteria.py kills each with a mutant.
    change = ["change.stale_set", "change.unchanged_set", "research.scope_change"]
    assert report.key_gaps == sorted(["precision.abstention_count", *change])
    gaps = len(report.key_gaps)
    assert (killed, total) == (len(REGISTRY) - gaps, len(REGISTRY) - gaps)


def test_each_mutant_names_a_registered_criterion() -> None:
    assert {c for m in MUTANTS.values() for c in m.kills} <= set(REGISTRY)
    assert {
        "double-send",
        "forged-excerpt",
        "whole-file-cite",
        "injection-obeyed",
        "price-zero",
        "status-merged",
        "gap-filled",
        "known-value-nulled",
    } <= set(MUTANTS)


def test_the_bundles_cover_the_five_kinds(reports: dict[str, SelfTestReport]) -> None:
    names = {c.bundle.split(":")[0] for c in reports["stillroom"].checks}
    assert {"oracle", "perturbed", "null", "lister", "hedger", "mutant"} <= names


def test_directional_relations_pass_the_oracles_and_fail_an_ignored_change(
    reports: dict[str, SelfTestReport],
) -> None:
    base, v2 = reports["stillroom"].oracle, reports["stillroom-v2"].oracle
    assert base is not None and v2 is not None
    maps = renaming(world("stillroom"), world("stillroom-v2"))
    check = relation_self_test(base, v2, key("stillroom"), key("stillroom-v2"), maps)
    assert check.ok, check.problems


def test_self_test_agents_are_never_registered_as_agents_under_test() -> None:
    assert not {"oracle", "null", "lister", "hedger"} & set(eval_agents().names())


def test_a_grader_that_cannot_fail_is_caught(tmp_path: Path) -> None:
    from aksha.evals.grade import Criterion, passed

    lenient = dict(REGISTRY)
    lenient["trap.T-STATUS"] = Criterion("trap.T-STATUS", "trap", lambda *_: passed("always"))
    report = self_test(world(), key(), open_store=OPEN, out=tmp_path, registry=lenient)
    bad = {c.bundle for c in report.checks if not c.ok}
    assert bad == {"mutant:status-merged"}
