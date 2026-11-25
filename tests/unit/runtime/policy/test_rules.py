from typing import Any

import pytest

from aksha.runtime.policy.rules import argument_at, broken_rule, rule_failure
from aksha.runtime.policy.types import ArgumentRule, SourceVersion

PEOPLE = {"people": SourceVersion("people", "v1", frozenset({"P-1"}))}


def _rule(matcher: Any, value: Any, path: str = "/x") -> ArgumentRule:
    return ArgumentRule("t", path, matcher, value, "because")


def test_argument_at_follows_a_json_pointer() -> None:
    args = {"a": {"b": [10, {"c~/d": 1}]}}
    assert argument_at(args, "/a/b/0") == (True, 10)
    assert argument_at(args, "/a/b/1/c~0~1d") == (True, 1)
    assert argument_at(args, "/a/zz") == (False, None)
    assert argument_at(args, "/a/b/9") == (False, None)
    assert argument_at(args, "/a/b/x") == (False, None)
    assert argument_at(args, "") == (True, args)


@pytest.mark.parametrize(
    ("value", "ok"),
    [("src/a.py", True), ("src", True), ("srcx/a.py", False), ("../src/a", False), (3, False)],
)
def test_path_under(value: Any, ok: bool) -> None:
    assert (rule_failure(_rule("path_under", "src"), {"x": value}, {}) is None) is ok


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("src/a.py", True),
        (".gitignore", True),
        (".git", False),
        (".git/hooks/pre-commit", False),
        ("src/../.git/config", False),
        ("/abs/.git", False),
    ],
)
def test_path_not_under(value: str, ok: bool) -> None:
    assert (rule_failure(_rule("path_not_under", ".git"), {"x": value}, {}) is None) is ok


@pytest.mark.parametrize(
    ("root", "value", "ok"),
    [
        ("src/", "src", True),
        ("./src", "src/a.py", True),
        ("src", "SRC/a.py", False),  # an allow-list is case-sensitive
        (".", "anything/here", True),
        ("/abs", "abs/a.py", False),  # a root that cannot be placed allows nothing
    ],
)
def test_path_under_compares_the_root_in_normal_form(root: str, value: str, ok: bool) -> None:
    assert (rule_failure(_rule("path_under", root), {"x": value}, {}) is None) is ok


@pytest.mark.parametrize(
    ("root", "value"),
    [(".git/", ".git"), ("./.git", ".git/config"), (".git", ".GIT/config"), ("/abs", "x")],
)
def test_path_not_under_compares_the_root_in_normal_form(root: str, value: str) -> None:
    assert rule_failure(_rule("path_not_under", root), {"x": value}, {}) is not None


def test_enum() -> None:
    rule = _rule("enum", ("a", "b"))
    assert rule_failure(rule, {"x": "a"}, {}) is None
    assert rule_failure(rule, {"x": "c"}, {}) is not None


def test_id_in_source() -> None:
    rule = _rule("id_in_source", "people")
    assert rule_failure(rule, {"x": "P-1"}, PEOPLE) is None
    assert "not in source 'people'" in str(rule_failure(rule, {"x": "P-9"}, PEOPLE))
    assert "no source" in str(rule_failure(rule, {"x": "P-1"}, {}))


def test_max_length_counts_strings_and_lists() -> None:
    rule = _rule("max_length", 3)
    assert rule_failure(rule, {"x": "abc"}, {}) is None
    assert rule_failure(rule, {"x": [1, 2, 3, 4]}, {}) is not None
    assert rule_failure(rule, {"x": 12345}, {}) is not None


def test_an_absent_argument_is_left_to_the_schema() -> None:
    assert rule_failure(_rule("enum", ("a",)), {}, {}) is None


def test_an_unknown_matcher_raises_so_the_gate_fails_closed() -> None:
    with pytest.raises(ValueError, match="no matcher"):
        rule_failure(_rule("lambda", None), {"x": 1}, {})


def test_broken_rule_returns_the_first_failing_rule() -> None:
    ok, bad = _rule("enum", ("a",)), _rule("max_length", 0)
    assert broken_rule((ok, bad), {"x": "a"}, {}) == (bad, "/x is 1 long, over 0")
    assert broken_rule((ok,), {"x": "a"}, {}) is None
