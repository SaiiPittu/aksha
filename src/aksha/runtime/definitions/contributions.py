"""How an extension's contributions are named, checked and merged with an agent's (ADR-P20 D6).

``CONTRIBUTIONS`` lists the ``Extension`` fields that hold contributions and ``local_name``
gives each item's local name. ``event_type_refusals`` checks an extension's event types
before anything is registered. ``merged`` and ``own_and_contributed`` build the full-name
mappings of a definition and refuse a full name two parts both declare.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

from aksha.runtime.artifacts.types import ArtifactKind, RelationKind
from aksha.runtime.definitions.errors import LoadRefusal, Refusals
from aksha.runtime.definitions.names import qualify
from aksha.runtime.definitions.types import Agent, Projection
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue, EventTypeSpec
from aksha.runtime.tasks.types import Task
from aksha.runtime.tools.types import ToolDecl

if TYPE_CHECKING:
    from aksha.runtime.definitions.loader import LoadedExtension

CONTRIBUTIONS = (
    "tools", "artifacts", "relations", "projections", "tasks", "sandbox_backends",
    "model_adapters", "sources", "systems", "environments", "egress",
)  # fmt: skip
"""Every ``Extension`` field that holds contributions, each namespaced by the extension."""


def local_name(item: object) -> str:
    """The local name of one contribution: a ToolDecl's spec name, a Task's id, else ``name``."""
    if isinstance(item, ToolDecl):
        return item.spec.name
    if isinstance(item, Task):
        return item.id
    return str(getattr(item, "name", ""))


def event_type_refusals(
    name: str, specs: Sequence[object], catalogue: Catalogue | None, out: Refusals
) -> None:
    """Refuse an extension's event types before anything is registered.

    ``event_type_invalid`` for a value that is not an ``EventTypeSpec``, a name outside the
    extension's namespace, another writer, or anything the catalogue refuses (without a
    catalogue, the core catalogue is checked against).
    """
    held = Catalogue(CORE_EVENT_TYPES) if catalogue is None else catalogue
    seen: set[str] = set()
    for spec in specs:
        if not isinstance(spec, EventTypeSpec):
            out.add("event_types", "event_type_invalid", f"{spec!r} is not an EventTypeSpec")
            continue
        if spec.name in seen:
            out.add("event_types", "duplicate_name", f"{spec.name!r} is declared twice")
            continue
        seen.add(spec.name)
        problem = held.extension_refusal(spec, extension=name)
        if problem is not None:
            out.add("event_types", "event_type_invalid", problem)


def merged[V](label: str, parts: Iterable[Mapping[str, V]], out: Refusals) -> dict[str, V]:
    """One mapping from ``parts``; a key that two parts both hold is a ``duplicate_name``."""
    whole: dict[str, V] = {}
    for part in parts:
        for key, value in part.items():
            if key in whole:
                out.add(
                    label, "duplicate_name", f"{key!r} is declared by the agent and an extension"
                )
                continue
            whole[key] = value
    return whole


def own_and_contributed(
    agent: Agent, exts: Sequence[LoadedExtension], out: Refusals
) -> tuple[dict[str, ArtifactKind], dict[str, RelationKind], dict[str, Projection]]:
    """The agent's kinds, relations and projections merged with its extensions', by full name."""
    kinds = merged(
        "artifacts",
        [{qualify(agent.name, k.name): k for k in agent.artifacts}, *(e.kinds for e in exts)],
        out,
    )
    relations = merged(
        "relations",
        [{qualify(agent.name, r.name): r for r in agent.relations}, *(e.relations for e in exts)],
        out,
    )
    projections = merged("projections", [e.projections for e in exts], out)
    return kinds, relations, projections


def merge_refusals(agent: Agent, module: str, exts: Sequence[LoadedExtension]) -> list[LoadRefusal]:
    """Every full name the agent and its extensions would both declare."""
    out = Refusals(module)
    own_and_contributed(agent, exts, out)
    return out.items
