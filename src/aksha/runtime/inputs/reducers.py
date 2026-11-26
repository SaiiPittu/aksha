"""Queued and pending input read from the log (ADR-P27 D2): pending is a projection.

An input is queued by its ``input.queued`` fact and consumed by the first
``input.consumed`` that names it: in a model step's result (a ``steer``), in the stop
decision that vetoed a finish (a ``follow_up``), or in the amendment's own transaction
(an ``amend``). Nothing else is stored, so a message typed before a crash is pending after
the restart. Every function here is pure.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from aksha.runtime.inputs.types import (
    DOCUMENT_FACTS,
    DOCUMENT_STORED,
    INPUT_CONSUMED,
    INPUT_QUEUED,
    RENDERED_LANES,
    KnownDocument,
    QueuedInput,
)
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.types import InstanceRef


def queued_inputs(view: LogView, run: str) -> tuple[QueuedInput, ...]:
    """Every input queued for ``run``, in log order."""
    out: list[QueuedInput] = []
    for e in view.events:
        if e.type == INPUT_QUEUED and e.run == run:
            p = e.payload
            instance = p.get("instance")
            out.append(QueuedInput(str(p["input_id"]), run,
                                   None if instance is None else str(instance),
                                   str(p["lane"]), str(p["text"]), e.seq))  # fmt: skip
    return tuple(out)


def consumptions(view: LogView, run: str) -> Mapping[str, str | None]:
    """Each consumed input of ``run`` to the step that consumed it (None for an amendment)."""
    out: dict[str, str | None] = {}
    for e in view.events:
        if e.type == INPUT_CONSUMED and e.run == run:
            ident = str(e.payload["input_id"])
            if ident not in out:
                step = e.payload.get("step_id")
                out[ident] = None if step is None else str(step)
    return MappingProxyType(out)


def _targets(item: QueuedInput, ref: InstanceRef) -> bool:
    return item.instance is None or item.instance == ref.instance


def pending_input(
    view: LogView, ref: InstanceRef, *, lane: str | None = None
) -> tuple[QueuedInput, ...]:
    """The unconsumed ``steer`` and ``follow_up`` inputs that target ``ref``, oldest first.

    An input with no instance targets every instance of its run until one consumes it.
    ``lane`` narrows to one lane.
    """
    used = consumptions(view, ref.run)
    return tuple(
        i
        for i in queued_inputs(view, ref.run)
        if i.lane in RENDERED_LANES
        and (lane is None or i.lane == lane)
        and i.input_id not in used
        and _targets(i, ref)
    )


def consumed_at(view: LogView, ref: InstanceRef, step: str) -> tuple[QueuedInput, ...]:
    """The inputs consumed for ``ref``'s model step ``step``, in queue order."""
    used = consumptions(view, ref.run)
    return tuple(i for i in queued_inputs(view, ref.run) if used.get(i.input_id) == step)


def consumed_by_step(view: LogView, ref: InstanceRef) -> Mapping[str, tuple[QueuedInput, ...]]:
    """Every input consumed for one of ``ref``'s model steps, by that step's ID."""
    prefix = f"{ref.run}/{ref.instance}/"
    used = consumptions(view, ref.run)
    out: dict[str, list[QueuedInput]] = {}
    for i in queued_inputs(view, ref.run):
        step = used.get(i.input_id)
        if step is not None and step.startswith(prefix):
            out.setdefault(step, []).append(i)
    return MappingProxyType({k: tuple(v) for k, v in out.items()})


def known_documents(view: LogView) -> Mapping[str, KnownDocument]:
    """Each input path's latest document, by path, from the ``document.stored`` facts."""
    out: dict[str, KnownDocument] = {}
    for e in view.events:
        if e.type == DOCUMENT_STORED:
            p = e.payload
            path = str(p["path"])
            out[path] = KnownDocument(path, str(p["doc_key"]), int(p["version"]), str(p["blob"]))
    return MappingProxyType(out)


def documents_after(view: LogView, seq: int) -> tuple[tuple[str, Mapping[str, object]], ...]:
    """The ``document.*`` facts committed right after the receipt at ``seq``, in order.

    An upload commits its receipt first, then its document facts, then the source change.
    """
    out: list[tuple[str, Mapping[str, object]]] = []
    for e in view.events:
        if e.seq <= seq:
            continue
        if e.type not in DOCUMENT_FACTS:
            break
        out.append((e.type, e.payload))
    return tuple(out)
