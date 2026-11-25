import pytest

from aksha.runtime.definitions.versions import parse_requirement, parse_version, satisfies


@pytest.mark.parametrize(
    ("spec", "version", "ok"),
    [
        (">=1.0,<2", "1.0", True),
        (">=1.0,<2", "1.7", True),
        (">=1.0,<2", "2.0", False),
        (">=2", "1.0", False),
        ("==1.0", "1.0", True),
        (">1.0", "1.0", False),
        ("<=1.1", "1.1", True),
    ],
)
def test_satisfies(spec: str, version: str, ok: bool) -> None:
    assert satisfies(spec, version) is ok


def test_parse_requirement_returns_clauses() -> None:
    assert parse_requirement(">=1.0, <2") == ((">=", (1, 0)), ("<", (2, 0)))


@pytest.mark.parametrize("bad", ["", "1.0", ">=x", "~=1.0", ">=1.0,,<2"])
def test_parse_requirement_refuses_malformed_specs(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_requirement(bad)


def test_parse_version_pads_the_minor() -> None:
    assert parse_version("2") == (2, 0) and parse_version("1.7") == (1, 7)
    with pytest.raises(ValueError):
        parse_version("v1")
