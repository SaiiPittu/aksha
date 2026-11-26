from pathlib import Path

import pytest

from aksha.evals.case import ChangeStep, case_from_toml, check_against_keys, load_suite
from aksha.evals.errors import CaseInvalid
from aksha.evals.runner import validate_suite
from tests.support.evals import SUITES, case, key


def test_a_planted_trap_case_names_its_trap_and_expects_it() -> None:
    c = case(origin="planted_trap", source="trap: T-DUP", expect=("traps.T-DUP",))
    assert c.trap_id == "T-DUP"
    with pytest.raises(CaseInvalid):
        case(origin="planted_trap", source="trap: T-DUP")
    with pytest.raises(CaseInvalid):
        case(origin="planted_trap", source="requirement: x", expect=("traps.T-DUP",))


def test_a_real_failure_is_a_bad_case_from_a_bug_or_a_run() -> None:
    case(kind="bad", origin="real_failure", source="bug: #12")
    case(kind="bad", origin="real_failure", source="run: R-0007 step compare[C1]/t03")
    with pytest.raises(CaseInvalid):
        case(kind="guard", origin="real_failure", source="bug: #12")
    with pytest.raises(CaseInvalid):
        case(kind="bad", origin="real_failure", source="review: row 22")


def test_a_control_case_is_hand_written() -> None:
    case(kind="control")
    with pytest.raises(CaseInvalid):
        case(kind="control", origin="planted_trap", source="trap: T-DUP", expect=("traps.T-DUP",))


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "great"},
        {"origin": "folklore"},
        {"category": "vibes"},
        {"relation": "sideways"},
        {"source": "somebody said so"},
        {"agents": ()},
        {"worlds": ()},
        {"criteria": ("a",), "should": ("a",)},
        {"k": 0},
    ],
)
def test_field_values_are_checked(bad: dict[str, object]) -> None:
    with pytest.raises(CaseInvalid):
        case(**bad)


def test_case_files_parse_with_change_steps() -> None:
    c = case_from_toml(
        {
            "id": "x",
            "kind": "guard",
            "origin": "hand_written",
            "source": "requirement: change",
            "agents": ["patent"],
            "worlds": ["stillroom"],
            "change_steps": [
                {"after": "episode.output_saved:packet/C1", "change_set": "demo", "restart": True}
            ],
            "k": 3,
        }
    )
    assert c.change_steps == (ChangeStep("episode.output_saved:packet/C1", "demo", True),)
    assert c.k == 3 and c.category == "capability"
    with pytest.raises(CaseInvalid):
        case_from_toml({"id": "x"})


def test_check_against_keys_refuses_a_trap_a_world_lacks() -> None:
    c = case(origin="planted_trap", source="trap: T-NONE", expect=("traps.T-NONE",))
    problems = check_against_keys(c, {"stillroom": key()})
    assert any("T-NONE is not in world stillroom" in p for p in problems)
    gaps = check_against_keys(case(expect=("counts.abstentions",)), {"stillroom": key()})
    assert gaps == ["missing: t-case: stillroom has no counts.abstentions"]


@pytest.mark.parametrize("name", ["scripted", "research", "patent"])
def test_every_shipped_suite_loads_and_holds_against_both_keys(name: str) -> None:
    suite = load_suite(SUITES / name)
    keys = {w: key(w) for w in suite.worlds}
    assert validate_suite(suite, keys) == []
    assert suite.cases


def test_an_unknown_criterion_is_refused(tmp_path: Path) -> None:
    (tmp_path / "suite.toml").write_text('[suite]\nname = "t"\n')
    (tmp_path / "a.toml").write_text(
        'id = "a"\nkind = "guard"\norigin = "hand_written"\nsource = "requirement: t"\n'
        'agents = ["scripted"]\nworlds = ["stillroom"]\ncriteria = ["research.vibes"]\n'
    )
    suite = load_suite(tmp_path)
    with pytest.raises(CaseInvalid) as e:
        validate_suite(suite, {"stillroom": key()})
    assert e.value.code == "unknown_criterion"


def test_a_case_naming_an_undefined_fault_profile_is_refused(tmp_path: Path) -> None:
    (tmp_path / "suite.toml").write_text('[suite]\nname = "t"\n')
    (tmp_path / "a.toml").write_text(
        'id = "a"\nkind = "guard"\norigin = "hand_written"\nsource = "requirement: t"\n'
        'agents = ["scripted"]\nworlds = ["stillroom"]\nfaults = "nope"\n'
    )
    with pytest.raises(CaseInvalid):
        load_suite(tmp_path)
