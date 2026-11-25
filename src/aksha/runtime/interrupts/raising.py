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

from collections.abc import Mapping
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
    declared: DeclaredInterrupt, args: Mapping[str, Any], *, run: str, instance: str
) -> InterruptRequest:
    """The interrupt an ``ask`` call raises on the instance whose Task declared ``declared``.

    ``question`` is one question (ID :data:`SINGLE_QUESTION`); ``questions`` (a list of
    ``{id, text}``) asks several, each answered on its own (S2). A ``choice`` lists
    ``options``.
    """
    listed = args.get("questions")
    if listed:
        questions = tuple(Question(str(q["id"]), str(q["text"])) for q in listed)
    else:
        questions = (Question(SINGLE_QUESTION, str(args["question"])),)
    options = tuple(str(o) for o in args.get("options", ()))
    return InterruptRequest(
        declared.kind, run=run, instance=instance, questions=questions, options=options,
        label=declared.label,
    )  # fmt: skip


def route_request(session: str, question: str, agents: tuple[str, ...]) -> InterruptRequest:
    """The session-scoped ``route`` question the request router raises when it abstains (S5)."""
    return InterruptRequest(
        "route", session=session, questions=(Question(SINGLE_QUESTION, question),),
        options=agents,
    )  # fmt: skip
