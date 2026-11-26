"""Classification options read from a saved artifact (ADR-P07 S1).

"What do customers complain about?" has no option list in advance. The workable shape is
two steps: a generation step saves a theme list with a citation per theme, then
classification labels each review against it. A question that declares ``options_from``
(an ``artifact_keys`` source) has its options read here, when the request is built: the
keys listed at the field of the kind's current version, sorted. The version's ref joins
the request's ``state_inputs``, so the options are recorded data with lineage, and a new
theme list changes the request hash.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from aksha.runtime.artifacts.versions import current
from aksha.runtime.models.errors import OptionsUnavailable
from aksha.runtime.models.types import QuestionSpec
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.keys import keys_of


def resolve_questions(
    view: LogView, questions: Sequence[QuestionSpec], namespace: str
) -> tuple[tuple[QuestionSpec, ...], tuple[str, ...]]:
    """Each question with its options resolved, and the artifact refs they were read from.

    ``namespace`` is the agent's name, for a kind named locally. Raises
    ``OptionsUnavailable`` when the artifact is not saved or lists under two options.
    """
    out: list[QuestionSpec] = []
    inputs: list[str] = []
    for q in questions:
        source = q.options_from
        if source is None:
            out.append(q)
            continue
        kind = source.name if "." in source.name else f"{namespace}.{source.name}"
        version = current(view, kind, None)
        options = keys_of(view, source, kind=kind)
        if version is None or options is None or len(options) < 2:
            message = f"{q.name}: {kind} at {source.path!r} lists no two options yet"
            raise OptionsUnavailable(message=message)
        out.append(replace(q, options=options, options_from=None))
        inputs.append(str(version.ref))
    return tuple(out), tuple(dict.fromkeys(inputs))
