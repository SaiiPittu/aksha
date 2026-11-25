"""The extension API version, and ``requires_api`` specifiers such as ``">=1.0,<2"`` (D2).

``API_VERSION`` is ``major.minor``: adding a public name bumps the minor version; removing
or changing one bumps the major. :mod:`aksha.runtime.api` re-exports it.

A specifier is comma-separated clauses, each an operator (``>=``, ``>``, ``<=``, ``<``,
``==``) and a ``major[.minor]`` version. A missing minor reads as 0.
"""

from __future__ import annotations

import re

API_VERSION = "1.1"
"""The version of :mod:`aksha.runtime.api`; the snapshot test pins its names to it.

1.0 is the first frozen snapshot (ADR-P20 S1). 1.1 added ``Extension.event_types`` and
``LogView.complete`` (both trailing fields with defaults).
"""

Version = tuple[int, int]
Clause = tuple[str, Version]

_CLAUSE = re.compile(r"^(>=|<=|==|>|<)\s*(\d+)(?:\.(\d+))?$")
_COMPARE = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def parse_version(text: str) -> Version:
    """``"1.2"`` -> ``(1, 2)``; ``"2"`` -> ``(2, 0)``. Raises ``ValueError`` otherwise."""
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?", text.strip())
    if not match:
        raise ValueError(f"not a major.minor version: {text!r}")
    return int(match.group(1)), int(match.group(2) or 0)


def parse_requirement(spec: str) -> tuple[Clause, ...]:
    """The clauses of ``spec``. Raises ``ValueError`` on an empty or malformed specifier."""
    clauses: list[Clause] = []
    for part in spec.split(","):
        match = _CLAUSE.match(part.strip())
        if not match:
            raise ValueError(f"malformed requires_api clause {part.strip()!r} in {spec!r}")
        op, major, minor = match.groups()
        clauses.append((op, (int(major), int(minor or 0))))
    return tuple(clauses)


def satisfies(spec: str, version: str) -> bool:
    """True when ``version`` meets every clause of ``spec``."""
    have = parse_version(version)
    return all(_COMPARE[op](have, want) for op, want in parse_requirement(spec))
