from aksha.runtime.invalidation.redline import redline
from aksha.runtime.invalidation.types import Hunk
from tests.support.invalidation import KINDS, kind

COMPARE = KINDS["p.compare"]


def test_keyed_rows_match_by_key_not_position() -> None:
    old = {"rows": [{"id": "F1", "state": "partial"}, {"id": "F2", "state": "none"}]}
    new = {"rows": [{"id": "F2", "state": "none"}, {"id": "F1", "state": "disclosed"}]}
    assert redline(COMPARE, old, new) == (Hunk("/rows/F1/state", "partial", "disclosed"),)


def test_added_and_removed_rows_and_fields() -> None:
    old = {"rows": [{"id": "F1"}], "note": "x"}
    new = {"rows": [{"id": "F1"}, {"id": "F3"}], "closest": "E"}
    assert redline(COMPARE, old, new) == (
        Hunk("/closest", None, "E"),
        Hunk("/note", "x", None),
        Hunk("/rows/F3", None, {"id": "F3"}),
    )


def test_unkeyed_lists_compare_by_position_and_equal_content_has_no_hunks() -> None:
    plain = kind("plain")
    assert redline(plain, {"a": [1, 2]}, {"a": [1, 3, 4]}) == (
        Hunk("/a/1", 2, 3),
        Hunk("/a/2", None, 4),
    )
    assert redline(plain, {"a": {"b": [1]}}, {"a": {"b": [1]}}) == ()


def test_redline_is_capped() -> None:
    plain = kind("plain")
    old = {f"k{i:02d}": i for i in range(50)}
    new = {k: v + 1 for k, v in old.items()}
    hunks = redline(plain, old, new, max_hunks=40)
    assert len(hunks) == 40 and hunks[0].path == "/k00"
