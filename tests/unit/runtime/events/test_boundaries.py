import pytest

from aksha.runtime.events.boundaries import BOUNDARIES, boundary
from aksha.runtime.events.errors import UnknownBoundary


def test_boundaries_are_the_closed_v2_list() -> None:
    assert set(BOUNDARIES) == {
        "U1", "U2a", "U2b", "U2c", "U2", "U3", "U4", "U5", "U6", "U7", "U8", "U9", "U10", "U11",
        "U12",
    }  # fmt: skip
    assert BOUNDARIES["U3"].name == "stop_decision"
    assert BOUNDARIES["U7"].name == "recovery"
    assert all(b.id == key and b.name and b.written_by for key, b in BOUNDARIES.items())


def test_boundary_looks_up_or_refuses() -> None:
    assert boundary("U2").name == "step_result"
    with pytest.raises(UnknownBoundary) as e:
        boundary("U42")
    assert e.value.code == "unknown_boundary"
