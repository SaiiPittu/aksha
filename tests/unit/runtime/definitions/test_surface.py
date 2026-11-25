import pytest

from aksha.runtime.definitions.surface import (
    api_surface,
    only_added_defaults,
    required_bump,
    stable_repr,
)


def test_api_surface_lists_every_public_name_with_its_signature() -> None:
    surface = api_surface()
    assert "API_VERSION" not in surface
    assert surface["Agent"].startswith("class Agent(")
    assert surface["source_keys"].startswith("def source_keys(")
    assert "requires_api" in surface["Agent"]


@pytest.mark.parametrize(
    ("old", "new", "bump"),
    [
        ({"a": "x"}, {"a": "x"}, "none"),
        ({"a": "x"}, {"a": "x", "b": "y"}, "minor"),
        ({"a": "x", "b": "y"}, {"a": "x"}, "major"),
        ({"a": "x"}, {"a": "x2"}, "major"),
    ],
)
def test_required_bump(old: dict[str, str], new: dict[str, str], bump: str) -> None:
    assert required_bump(old, new) == bump


def test_stable_repr_sorts_sets_and_recurses_into_dataclasses() -> None:
    from aksha.runtime.tools.types import RetryPolicy

    assert stable_repr(frozenset({"b", "a"})) == "frozenset({'a', 'b'})"
    assert stable_repr(frozenset()) == "frozenset()"
    assert stable_repr((1,)) == "(1,)" and stable_repr((1, 2)) == "(1, 2)"
    assert "retry_on=frozenset({'timeout', 'transient'})" in stable_repr(RetryPolicy())


def test_a_new_defaulted_trailing_field_is_a_minor_bump() -> None:
    old = {"X": "class X(a: 'int', b: 'str' = 'x')"}
    added = {"X": "class X(a: 'int', b: 'str' = 'x', c: 'tuple[int, ...]' = ())"}
    required = {"X": "class X(a: 'int', b: 'str' = 'x', c: 'int')"}
    reordered = {"X": "class X(c: 'int' = 0, a: 'int', b: 'str' = 'x')"}
    assert required_bump(old, added) == "minor"
    assert required_bump(old, required) == "major"
    assert required_bump(old, reordered) == "major"
    assert only_added_defaults("def f(a: 'int') -> 'str'", "def f(a: 'int', b: 'int' = 1) -> 'str'")
    assert not only_added_defaults("def f(a: 'int') -> 'str'", "def f(a: 'int') -> 'int'")
