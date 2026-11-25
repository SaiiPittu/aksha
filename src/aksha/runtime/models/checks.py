"""The loader's refusals of a model alias declaration (ADR-P07 D2, D5, S1). Pure.

The binder (``routing.bind_aliases``) checks an alias against the adapter a run mode
binds; these checks need no adapter, so the loader runs them on every agent:

1. ``generate_not_generation``: an alias named ``generate`` is the implicit generation
   alias, so it is of the generation class.
2. ``on_exhausted_unknown``: ``on_exhausted`` is ``unknown``, ``review`` or ``fail``.
3. ``fields_not_extraction``, ``questions_not_classification``,
   ``allow_adapted_not_classification``: each part belongs to one class.
4. ``adapted_gates_effect``: an adapted answer never gates an external effect (D5).
5. ``field_type_unknown``, ``question_type_unknown``, ``spec_name_duplicate``.
6. ``options_exactly_one``: a choice or score question has exactly one of ``options``
   and ``options_from`` (S1); ``boolean_has_options``: a boolean question has neither;
   ``options_too_few``, ``options_duplicate``; ``options_from_not_artifact``: options
   are read from a saved artifact's field.
7. ``threshold_invalid``: every threshold is a fraction from 0 to 1, ``reject_at`` is
   under ``accept_at``, and ``margin`` (when set) is a fraction too.
"""

from __future__ import annotations

from collections.abc import Iterable

from aksha.runtime.models.types import (
    FIELD_TYPES,
    IMPLICIT_GENERATION_ALIAS,
    ON_EXHAUSTED,
    QUESTION_TYPES,
    ConfidenceThresholds,
    ModelAlias,
    QuestionSpec,
)

Problem = tuple[str, str]
"""``(code, message)``; the loader adds the field and the module."""


def _fraction(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= float(value) <= 1


def threshold_problems(where: str, t: ConfidenceThresholds) -> list[Problem]:
    """``threshold_invalid`` for a value outside 0 to 1 or a band that is empty."""
    values = {"accept_at": t.accept_at, "reject_at": t.reject_at, "null_at": t.null_at}
    if t.margin is not None:
        values["margin"] = t.margin
    bad = sorted(k for k, v in values.items() if not _fraction(v))
    if bad:
        return [("threshold_invalid", f"{where}: {', '.join(bad)} must be fractions 0 to 1")]
    if t.reject_at >= t.accept_at:
        return [("threshold_invalid", f"{where}: reject_at is under accept_at")]
    return []


def _question_problems(where: str, q: QuestionSpec) -> list[Problem]:
    if q.type not in QUESTION_TYPES:
        return [("question_type_unknown", f"{where}: no question type {q.type!r}")]
    out: list[Problem] = []
    if q.type == "boolean":
        if q.options or q.options_from is not None:
            out.append(("boolean_has_options", f"{where}: a boolean question lists no options"))
    elif bool(q.options) == (q.options_from is not None):
        message = f"{where}: give exactly one of options and options_from"
        out.append(("options_exactly_one", message))
    elif q.options_from is not None and q.options_from.origin != "artifact":
        message = f"{where}: options_from reads a saved artifact (artifact_keys)"
        out.append(("options_from_not_artifact", message))
    elif q.options and len(q.options) < 2:
        out.append(("options_too_few", f"{where}: a {q.type} question lists two or more options"))
    elif len(set(q.options)) != len(q.options):
        out.append(("options_duplicate", f"{where}: an option is listed twice"))
    out += threshold_problems(where, q.thresholds)
    if q.adapted is not None:
        out += threshold_problems(f"{where} (adapted)", q.adapted)
    return out


def alias_problems(alias: ModelAlias) -> list[Problem]:
    """Every refusal of one alias declaration, in the order of the module doc."""
    name, cls = alias.name, alias.model_class
    out: list[Problem] = []
    if name == IMPLICIT_GENERATION_ALIAS and cls != "generation":
        out.append(("generate_not_generation", f"{name!r} is the implicit generation alias"))
    if alias.on_exhausted is not None and alias.on_exhausted not in ON_EXHAUSTED:
        message = f"{name!r}: on_exhausted is one of {sorted(ON_EXHAUSTED)}"
        out.append(("on_exhausted_unknown", message))
    if alias.fields and cls != "extraction":
        out.append(("fields_not_extraction", f"{name!r}: only an extraction alias has fields"))
    if alias.questions and cls != "classification":
        message = f"{name!r}: only a classification alias has questions"
        out.append(("questions_not_classification", message))
    if alias.allow_adapted and cls != "classification":
        message = f"{name!r}: only a classification alias allows an adapted classifier"
        out.append(("allow_adapted_not_classification", message))
    if alias.allow_adapted and alias.gates_effect:
        message = f"{name!r}: an adapted answer never gates an external effect"
        out.append(("adapted_gates_effect", message))
    names = [f.name for f in alias.fields] + [q.name for q in alias.questions]
    if len(set(names)) != len(names):
        out.append(("spec_name_duplicate", f"{name!r}: a field or question is named twice"))
    for f in alias.fields:
        if f.type not in FIELD_TYPES:
            out.append(("field_type_unknown", f"{name}/{f.name}: no field type {f.type!r}"))
        out += threshold_problems(f"{name}/{f.name}", f.thresholds)
    for q in alias.questions:
        out += _question_problems(f"{name}/{q.name}", q)
    return out


def aliases_problems(aliases: Iterable[ModelAlias]) -> list[Problem]:
    """:func:`alias_problems` for each alias, in declaration order."""
    return [p for a in aliases for p in alias_problems(a)]
