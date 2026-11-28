"""Raising an interrupt (ADR-P17 D1, D2): the ``interrupt.raised`` fact, and the ``ask`` tool.

Each kind has one raiser. ``input`` and ``choice`` come from the implicit ``ask`` tool on
a Task that declares an interrupt (:func:`ask_request`); ``approval`` from a tool step that
requests approval (``approvals.request_approval``); ``budget`` from stop-policy evaluation
(``stop_policy/core.py``); ``review`` from invalidation (``invalidation/revision.py``);
``route`` from the request router's classification step when it abstains (S5).

A tool step returns the fact in its ``StepOutput.events``, so the interrupt commits with
its ``step.completed`` (U2) and the next stop decision only records the HOLD. Nothing here
appends. Pure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.interrupts.reducers import INTERRUPT_RAISED
from aksha.runtime.interrupts.types import DeclaredInterrupt, InterruptRequest, Question

SINGLE_QUESTION = "q1"
"""The ID of the one question of an ``ask`` call that lists no ``questions``."""


def raised(request: InterruptRequest, interrupt_id: str, *, step: str | None = None) -> NewEvent:
    """The ``interrupt.raised`` fact for ``request`` under ``interrupt_id``."""
    payload: dict[str, Any] = {"interrupt": interrupt_id, "kind": request.kind}
    if request.questions:
        payload["questions"] = [q.as_json() for q in request.questions]
    if request.options:
        payload["options"] = list(request.options)
    if request.label is not None:
        payload["label"] = request.label
    for k, v in request.extra.items():
        if k in payload:
            raise ValueError(f"{k!r} is a field every interrupt has; it cannot be extra")
        payload[k] = v
    return NewEvent(
        INTERRUPT_RAISED,
        payload,
        run=request.run,
        instance=request.instance,
        session=request.session,
        step=step,
    )


def ask_request(
    declared: DeclaredInterrupt,
    args: Mapping[str, Any],
    *,
    run: str,
    instance: str,
    options: Sequence[tuple[str, str]] | None = None,
    drawn_from: Sequence[str] = (),
) -> InterruptRequest:
    """The interrupt an ``ask`` call raises on the instance whose Task declared ``declared``.

    ``question`` is one question (ID :data:`SINGLE_QUESTION`); ``questions`` (a list of
    ``{id, text}``) asks several, each answered on its own (S2). A ``choice`` lists
    ``options``: the model's, or, when the Task declares them in code, ``options`` (the
    ``(code, label)`` pairs its ``DeclaredInterrupt.options`` gave), and then a model's own
    ``options`` argument is refused (``ValueError``). Declared options also record their
    labels (``option_labels``), whether any subset may be chosen (``multiple``) and
    ``drawn_from``, the refs of the Task's declared reads the options were built from, so
    an answer given after one of them changed is not saved (``tasks.choices.is_stale``).
    """
    listed = args.get("questions")
    if listed:
        questions = tuple(Question(str(q["id"]), str(q["text"])) for q in listed)
    else:
        questions = (Question(SINGLE_QUESTION, str(args["question"])),)
    extra: dict[str, Any] = {}
    if declared.options is not None:
        if "options" in args:
            raise ValueError("this Task's options are set by its definition; ask with the "
                             "question only")  # fmt: skip
        pairs = tuple((str(c), str(label)) for c, label in (options or ()))
        codes = tuple(c for c, _ in pairs)
        extra = {"option_labels": [label for _, label in pairs], "multiple": declared.multiple,
                 "drawn_from": [str(d) for d in drawn_from]}  # fmt: skip
    else:
        codes = tuple(str(o) for o in args.get("options", ()))
    return InterruptRequest(
        declared.kind, run=run, instance=instance, questions=questions, options=codes,
        label=declared.label, extra=extra,
    )  # fmt: skip


def route_request(session: str, question: str, agents: tuple[str, ...]) -> InterruptRequest:
    """The session-scoped ``route`` question the request router raises when it abstains (S5)."""
    return InterruptRequest(
        "route", session=session, questions=(Question(SINGLE_QUESTION, question),),
        options=agents,
    )  # fmt: skip
