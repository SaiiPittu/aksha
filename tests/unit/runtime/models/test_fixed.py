"""Fixed-point probabilities and decimal strings at the event boundary (ADR-P07 D4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from aksha.runtime.models.fixed import decimal_string, distribution_ppm, ppm, top_two


def test_ppm_reads_the_fraction_as_written() -> None:
    assert ppm(0.9) == 900_000
    assert ppm(0.85) == 850_000
    assert ppm(1) == 1_000_000 and ppm(0) == 0
    assert ppm(0.1 + 0.2) == 300_000  # 0.30000000000000004 rounds to the nearest ppm
    assert ppm(Decimal("0.0000005")) == 0  # half to even
    assert ppm(Decimal("0.0000015")) == 2


@pytest.mark.parametrize("bad", [-0.01, 1.000001, float("nan"), float("inf"), True])
def test_ppm_refuses_what_is_not_a_probability(bad: float) -> None:
    with pytest.raises(ValueError):
        ppm(bad)


def test_a_distribution_sums_to_one_million_and_sorts_highest_first() -> None:
    dist = distribution_ppm({"b": 1 / 3, "a": 1 / 3, "c": 1 / 3})
    assert sum(p for _, p in dist) == 1_000_000
    # the remainder goes to the earliest option by name, so the bytes never vary
    assert dist == (("a", 333_334), ("b", 333_333), ("c", 333_333))
    assert distribution_ppm({"yes": 0.2, "no": 0.6}) == (("no", 750_000), ("yes", 250_000))


def test_a_distribution_with_no_mass_or_a_negative_weight_is_refused() -> None:
    with pytest.raises(ValueError):
        distribution_ppm({"a": 0.0, "b": 0.0})
    with pytest.raises(ValueError):
        distribution_ppm({"a": -0.1, "b": 1.1})
    with pytest.raises(ValueError):
        distribution_ppm({})


def test_top_two_reads_a_sorted_distribution() -> None:
    assert top_two((("a", 600_000), ("b", 400_000))) == (("a", 600_000), 400_000)
    assert top_two((("a", 1_000_000),)) == (("a", 1_000_000), 0)
    assert top_two(()) is None


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (12, "12"),
        (Decimal("12.50"), "12.50"),
        ("12.50", "12.50"),
        ("-0.5", "-0.5"),
        (12.5, "12.5"),
        (0.1, "0.1"),
        (1e20, "100000000000000000000"),
        (Decimal("1E+3"), "1000"),
    ],
)
def test_a_number_value_is_a_decimal_string(value: object, text: str) -> None:
    assert decimal_string(value) == text


@pytest.mark.parametrize("bad", ["12,50", "1e3", "", " 1", "NaN", True, None, float("nan")])
def test_a_value_that_is_not_a_plain_decimal_is_refused(bad: object) -> None:
    with pytest.raises(ValueError):
        decimal_string(bad)
