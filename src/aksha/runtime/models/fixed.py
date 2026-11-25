"""Fixed-point numbers at the event boundary: parts per million and decimal strings (D4).

The event store refuses floats in hashed payloads (ADR-P01 H3), so every probability,
``p_null`` and confidence a fast-class answer carries is an integer in parts per million,
and every ``number`` field's value is a decimal string. Adapters convert at their edge
with these functions; thresholds are converted once, at load. Two runs then write the
same bytes, and a threshold check compares integers.

A float is read through its shortest ``repr`` (``0.9`` is ``"0.9"``), never through its
binary expansion, so ``ppm(0.9)`` is exactly 900000.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from aksha.runtime.models.types import PPM

_DECIMAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")


def _decimal(value: object) -> Decimal:
    """``value`` as an exact ``Decimal``; floats through their shortest repr."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{value!r} is not a number")
    if isinstance(value, Decimal):
        out = value
    elif isinstance(value, int):
        out = Decimal(value)
    elif isinstance(value, float):
        out = Decimal(repr(value))
    elif isinstance(value, str):
        if not _DECIMAL.match(value):
            raise ValueError(f"{value!r} is not a plain decimal")
        out = Decimal(value)
    else:
        raise ValueError(f"{type(value).__name__} is not a number")
    if not out.is_finite():
        raise ValueError(f"{value!r} is not finite")
    return out


def ppm(fraction: float | int | Decimal) -> int:
    """A probability (0 to 1) as an integer in parts per million, rounded half to even."""
    try:
        value = _decimal(fraction)
    except (ValueError, InvalidOperation) as e:
        raise ValueError(f"{fraction!r} is not a probability") from e
    if value < 0 or value > 1:
        raise ValueError(f"{fraction!r} is outside 0 to 1")
    return int((value * PPM).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def distribution_ppm(weights: Mapping[str, float | int | Decimal]) -> tuple[tuple[str, int], ...]:
    """Normalise ``weights`` to parts per million that sum to exactly 1,000,000.

    Each option gets its floor; the remaining parts go one each to the options with the
    largest remainders, ties broken by option name, so the result never varies. Sorted
    by probability, highest first, then by option name.
    """
    if not weights:
        raise ValueError("a distribution needs at least one option")
    exact = {k: _decimal(v) for k, v in weights.items()}
    if any(v < 0 for v in exact.values()):
        raise ValueError(f"a distribution has no negative weights: {dict(weights)}")
    total = sum(exact.values(), Decimal(0))
    if total == 0:
        raise ValueError("a distribution needs some weight")
    scaled = {k: v * PPM / total for k, v in exact.items()}
    parts = {k: int(v) for k, v in scaled.items()}
    left = PPM - sum(parts.values())
    for k in sorted(scaled, key=lambda k: (-(scaled[k] - parts[k]), k))[:left]:
        parts[k] += 1
    return tuple(sorted(parts.items(), key=lambda kv: (-kv[1], kv[0])))


def top_two(dist: tuple[tuple[str, int], ...]) -> tuple[tuple[str, int], int] | None:
    """The top option and its parts, with the runner-up's parts (0 alone); None if empty."""
    if not dist:
        return None
    ordered = sorted(dist, key=lambda kv: (-kv[1], kv[0]))
    return ordered[0], (ordered[1][1] if len(ordered) > 1 else 0)


def decimal_string(value: object) -> str:
    """A ``number`` field's value as a plain decimal string: ``"12.50"``, ``"-0.5"``.

    A string is kept as written when it is already plain (trailing zeros matter to a
    price); every other number is written without an exponent.
    """
    if isinstance(value, str):
        _decimal(value)
        return value
    number = _decimal(value)
    text = format(number, "f")
    if "." in text and isinstance(value, float):
        text = text.rstrip("0").rstrip(".")
    return text
