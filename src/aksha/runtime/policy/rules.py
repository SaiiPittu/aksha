"""Argument matchers for ``ArgumentRule`` (ADR-P11 D7): a closed set, each a pure function.

Matchers: ``path_under``, ``path_not_under``, ``enum``, ``id_in_source`` and
``max_length``. A matcher sees only the one argument value, the rule's parameter and the
named source versions; it cannot read anything else, and it can only refuse.

A rule on an argument that is absent holds: whether the argument must be there is the
schema's job (layer 5). A path that is absolute or climbs out of the workspace with
``..`` fails both path matchers, so a rule fails closed on a path it cannot place. Both
the argument and the rule's root are compared in normal form (``policy/paths.py``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from aksha.runtime.policy.paths import is_under, normalised_path
from aksha.runtime.policy.types import ArgumentRule, SourceVersion

Sources = Mapping[str, SourceVersion]
MatcherFn = Callable[[Any, ArgumentRule, Sources], str | None]


def argument_at(args: Mapping[str, Any], pointer: str) -> tuple[bool, Any]:
    """``(True, value)`` at JSON pointer ``pointer`` in ``args``, or ``(False, None)``."""
    value: Any = args
    if pointer == "":
        return True, value
    for raw in pointer.split("/")[1:]:
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, list | tuple) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False, None
    return True, value


def _path_under(value: Any, rule: ArgumentRule, sources: Sources) -> str | None:
    path = normalised_path(value) if isinstance(value, str) else None
    if path is None or not is_under(path, str(rule.value), fold_case=False):
        return f"{rule.path} must lie under {rule.value!r}"
    return None


def _path_not_under(value: Any, rule: ArgumentRule, sources: Sources) -> str | None:
    path = normalised_path(value) if isinstance(value, str) else None
    unplaceable = normalised_path(str(rule.value)) is None  # a deny-list fails closed
    if path is None or unplaceable or is_under(path, str(rule.value), fold_case=True):
        return f"{rule.path} must not lie under {rule.value!r}"
    return None


def _enum(value: Any, rule: ArgumentRule, sources: Sources) -> str | None:
    allowed = rule.value if isinstance(rule.value, tuple) else (rule.value,)
    return None if value in allowed else f"{rule.path} must be one of {list(allowed)}"


def _id_in_source(value: Any, rule: ArgumentRule, sources: Sources) -> str | None:
    source = sources.get(str(rule.value))
    if source is None:
        return f"no source {rule.value!r} is available to check {rule.path}"
    if not isinstance(value, str) or not source.holds(value):
        return f"{rule.path} is not in source {rule.value!r} at version {source.version}"
    return None


def _max_length(value: Any, rule: ArgumentRule, sources: Sources) -> str | None:
    if not isinstance(value, str | list | tuple):
        return f"{rule.path} has no length"
    if not isinstance(rule.value, int) or len(value) > rule.value:
        return f"{rule.path} is {len(value)} long, over {rule.value}"
    return None


MATCHER_FNS: Mapping[str, MatcherFn] = MappingProxyType(
    {
        "path_under": _path_under,
        "path_not_under": _path_not_under,
        "enum": _enum,
        "id_in_source": _id_in_source,
        "max_length": _max_length,
    }
)
"""Each matcher's function, by name. Closed: a new matcher is a reviewed runtime change."""


def rule_failure(rule: ArgumentRule, args: Mapping[str, Any], sources: Sources) -> str | None:
    """None when ``rule`` holds for ``args``, else why not. Raises on an unknown matcher."""
    fn = MATCHER_FNS.get(rule.matcher)
    if fn is None:
        raise ValueError(f"no matcher named {rule.matcher!r}")
    found, value = argument_at(args, rule.path)
    return fn(value, rule, sources) if found else None


def broken_rule(
    rules: Sequence[ArgumentRule], args: Mapping[str, Any], sources: Sources
) -> tuple[ArgumentRule, str] | None:
    """The first of ``rules`` that fails for ``args``, with why; None when all hold."""
    for rule in rules:
        failure = rule_failure(rule, args, sources)
        if failure is not None:
            return rule, failure
    return None
