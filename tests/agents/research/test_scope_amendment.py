"""The fake's scope rule for a written amendment: the person's later word wins."""

from __future__ import annotations

from aksha.agents.research import fakes
from aksha.agents.research.fakes import Result, members

LINE = (
    "Scope amendment, 26 Nov 2026: the research covers the European Union and the United Kingdom."
)


def _note(*lines: str) -> Result:
    data = {"lines": [{"n": i, "text": t} for i, t in enumerate(lines)]}
    return Result(
        "R-1/scope/t02/c01",
        "read_note",
        {},
        data,
        None,
        tuple(f"EX-{i:04d}" for i, _ in enumerate(lines, 1)),
    )


def test_an_amendment_line_names_every_region_it_mentions() -> None:
    found = fakes._amendment([_note("Ranked, most important first.", LINE)])
    assert found is not None
    assert (found["normalised"], found["basis"]) == ("EU+UK", "note")
    assert found["cite"] == {"span": "EX-0002", "quote": LINE}


def test_a_line_that_is_no_amendment_changes_nothing() -> None:
    assert fakes._amendment([_note("Privacy story for EU works councils.")]) is None


def test_joined_regions_count_every_member() -> None:
    assert members("EU+UK") == members("EU") | {"UK"}
    assert members("US") == {"US"}
