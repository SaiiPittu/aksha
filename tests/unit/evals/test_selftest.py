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
    unkeyed = [c for c in REGISTRY if c.startswith("trap.") and c[5:] not in key(name).traps]
    assert report.key_gaps == sorted(["precision.abstention_count", *change, *unkeyed])
    gaps = len(report.key_gaps)
    assert (killed, total) == (len(REGISTRY) - gaps, len(REGISTRY) - gaps)


HELD_OUT = ("cindral", "tilldew", "pulvane", "quillmere", "brackwen", "rimefold", "kelasora",
            "vantlow")  # fmt: skip


@pytest.mark.parametrize("name", HELD_OUT)
def test_every_world_passes_its_self_tests(tmp_path: Path, name: str) -> None:
    """The graders and the oracle read every world's key alone, whatever its shape."""
    report = self_test(world(name), key(name), open_store=OPEN, out=tmp_path, registry=REGISTRY)
    assert [(c.bundle, c.problems) for c in report.checks if not c.ok] == []


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


NEW_TRAPS = (
    ("quillmere", "trap.T-NOTEBOOK"),
    ("quillmere", "trap.T-UNITS"),
    ("quillmere", "trap.T-OBVIOUS-4"),
    ("quillmere", "trap.T-OBVIOUS-5"),
    ("pulvane", "trap.T-PARAMCONFLICT"),
    ("kelasora", "trap.T-REGION"),
    ("kelasora", "trap.T-UNIT"),
    ("vantlow", "trap.T-UNIT"),
)


@pytest.mark.parametrize(("name", "trap"), NEW_TRAPS)
def test_each_trap_check_passes_the_oracle_and_a_mutant_fails_it(
    tmp_path: Path, name: str, trap: str
) -> None:
    report = self_test(world(name), key(name), open_store=OPEN, out=tmp_path, registry=REGISTRY)
    assert trap not in report.key_gaps
    assert trap in report.killed


def test_no_grader_names_a_field_one_world_extracted() -> None:
    """The graders, the oracle and the adapter read field names from each key, never one world.

    The words below are the extracted fields of the first world's candidate; a grader that
    names one cannot pass on another world's candidate whatever the agent extracts.
    """
    import aksha.evals as evals

    root = Path(evals.__file__).parent
    first_world = ("quiet_window", "walk_test", '"gates"', "rejected_alternative",
                   "conception_date", '"C1"')  # fmt: skip
    for path in [*root.joinpath("criteria").glob("*.py"), *root.joinpath("selftest").glob("*.py"),
                 root / "agents" / "patent.py", root / "judge.py"]:  # fmt: skip
        text = path.read_text(encoding="utf-8")
        assert [w for w in first_world if w in text] == [], path.name
