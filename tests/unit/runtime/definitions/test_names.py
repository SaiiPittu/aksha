import pytest

from aksha.runtime.definitions.names import is_local_name, qualify, resolve_kind


def test_qualify_prefixes_the_namespace() -> None:
    assert qualify("toy", "line") == "toy.line"


def test_resolve_kind_keeps_full_names_and_records() -> None:
    assert resolve_kind("line", "toy") == "toy.line"
    assert resolve_kind("other.line", "toy") == "other.line"
    assert resolve_kind("record:notes", "toy") == "record:notes"


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("line", True),
        ("save_line", True),
        ("a.b", False),
        ("", False),
        ("Line", False),
        ("1x", False),
    ],
)
def test_is_local_name(name: str, ok: bool) -> None:
    assert is_local_name(name) is ok
