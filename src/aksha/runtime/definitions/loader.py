"""The agent loader: ``discover``, ``load_extensions`` and ``load_agents`` (ADR-P20 D6).

One fixed order, all before any event is written: extensions first, then agents, each in
sorted order. For an extension: find its folder, scan its sources, import its
``EXTENSION``, check its name and ``requires_api``, namespace its contributions, check its
event types. For an agent: scan, import its ``AGENT``, check its name, load the extensions
it names, run ``validate_agent``, check its prompt files and source roots; then, across
agents, a name shared by an agent and an extension, and the kinds one agent reads from
another. Any refusal raises ``LoadError`` with all of them, and nothing starts.

Only when every definition loads are the extensions' event types registered in the
catalogue passed in (``Catalogue.register_extension``), so a refused load leaves the
catalogue as it was.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

from aksha.runtime.artifacts.types import ArtifactKind, RelationKind
from aksha.runtime.context.policy import policy_hash
from aksha.runtime.context.types import ContextPolicy
from aksha.runtime.definitions.contributions import (
    CONTRIBUTIONS,
    event_type_refusals,
    local_name,
    merge_refusals,
    own_and_contributed,
)
from aksha.runtime.definitions.errors import LoadError, LoadRefusal, Refusals
from aksha.runtime.definitions.hashing import FileHashes, definition_hash, file_hashes
from aksha.runtime.definitions.modules import SourceFinding, import_value, scan_sources
from aksha.runtime.definitions.names import qualify
from aksha.runtime.definitions.tree_checks import walk_tasks
from aksha.runtime.definitions.types import Agent, Extension, Projection
from aksha.runtime.definitions.validate import (
    api_refusals,
    cross_agent_refusals,
    name_refusals,
    validate_agent,
)
from aksha.runtime.definitions.versions import API_VERSION
from aksha.runtime.events.catalogue import Catalogue, EventTypeSpec
from aksha.runtime.models.types import ModelAlias
from aksha.runtime.tools.checks import spec_refusals
from aksha.runtime.tools.loader_added import LOADER_ADDED_TOOLS
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import ToolDecl


@dataclass(frozen=True)
class LoadedExtension:
    """An extension that passed every check, with its contributions under their full names."""

    ext: Extension
    folder: Path
    file_hashes: FileHashes
    tools: Mapping[str, ToolDecl] = field(default_factory=dict)
    kinds: Mapping[str, ArtifactKind] = field(default_factory=dict)
    relations: Mapping[str, RelationKind] = field(default_factory=dict)
    projections: Mapping[str, Projection] = field(default_factory=dict)
    event_types: tuple[EventTypeSpec, ...] = ()


@dataclass(frozen=True)
class LoadedDefinition:
    """An agent definition that passed every check: what a run is started from.

    ``tools`` is keyed by the name a Task uses (an own tool's local name, ``ext.tool`` for
    a contributed one) and holds the tools the agent lists, loader-added tools aside, with
    any ``ToolUse`` retry and timeout overrides applied.
    ``registry`` is the ADR-P10 tool registry built from them, reserved tools included; its
    hash is part of ``definition_hash``. Every other mapping is keyed by full name.
    """

    agent: Agent
    module: str
    folder: Path
    extensions: tuple[LoadedExtension, ...]
    tools: Mapping[str, ToolDecl]
    registry: ToolRegistry
    kinds: Mapping[str, ArtifactKind]
    relations: Mapping[str, RelationKind]
    projections: Mapping[str, Projection]
    model_aliases: Mapping[str, ModelAlias]
    definition_hash: str
    file_hashes: FileHashes


def with_context_policy(loaded: LoadedDefinition, policy: ContextPolicy) -> LoadedDefinition:
    """``loaded`` with its agent's context policy replaced, and the definition hash recomputed.

    The context policy is in the definition hash (ADR-P08 D9), so a run under the new policy
    is a run of another definition: an eval arm that changes it is never pooled with runs of
    the shipped one, and a session started under one never resumes under the other.
    """
    agent = replace(loaded.agent, context_policy=policy)
    new_hash = definition_hash(loaded.folder, loaded.extensions, API_VERSION,
                             tools_hash=loaded.registry.canonical_hash(),
                             context_hash=policy_hash(policy))  # fmt: skip
    return replace(loaded, agent=agent, definition_hash=new_hash)


def discover(agent_paths: Sequence[Path], extension_paths: Sequence[Path]) -> tuple[Path, ...]:
    """Every extension folder, then every agent folder (one holding ``agent.py``), each sorted."""
    extensions = sorted(
        (
            p
            for root in extension_paths
            if root.is_dir()
            for p in root.iterdir()
            if p.is_dir() and (p / "__init__.py").is_file()
        ),
        key=lambda p: (p.name, str(p)),
    )
    agents = sorted(
        (
            p
            for root in agent_paths
            if root.is_dir()
            for p in root.iterdir()
            if p.is_dir() and (p / "agent.py").is_file()
        ),
        key=lambda p: (p.name, str(p)),
    )
    return (*extensions, *agents)


def load_extensions(
    names: Iterable[str], search: Sequence[Path], *, catalogue: Catalogue | None = None
) -> tuple[LoadedExtension, ...]:
    """Load each named extension from the first search path that has it. Raises ``LoadError``.

    With ``catalogue``, each extension's event types are checked against it and, once all
    load, registered in it.
    """
    loaded, refusals = _load_named(names, search, catalogue)
    if refusals:
        raise LoadError(tuple(refusals))
    if catalogue is not None:
        _register_event_types(loaded, catalogue)
    return loaded


def load_agents(
    paths: Sequence[Path], search: Sequence[Path], *, catalogue: Catalogue | None = None
) -> tuple[LoadedDefinition, ...]:
    """Load, check and hash every agent folder in ``paths``; raise ``LoadError`` on a refusal.

    Writes no event: it runs before any ``run.started``. With ``catalogue``, the event
    types of every extension loaded are registered in it once everything loads.
    """
    refusals: list[LoadRefusal] = []
    found: list[tuple[Agent, str, Path, tuple[LoadedExtension, ...]]] = []
    seen: set[str] = set()
    for folder in sorted(paths, key=lambda p: (p.name, str(p))):
        module = f"{folder.name}/agent.py"
        agent, problems = _import_agent(folder, module)
        refusals += problems
        if agent is None:
            continue
        if agent.name in seen:
            out = Refusals(module)
            out.add("name", "duplicate_name", f"another loaded agent is named {agent.name!r}")
            refusals += out.items
            continue
        seen.add(agent.name)
        present = [n for n in agent.extensions if _find(n, search) is not None]
        exts, problems = _load_named(present, search, catalogue)
        if problems:
            refusals += problems
            continue
        problems = validate_agent(agent, module=module, extensions=exts)
        problems += _file_refusals(agent, folder, module)
        problems += merge_refusals(agent, module, exts)
        refusals += problems
        if not problems:
            found.append((agent, module, folder, exts))
    every_ext = {e.ext.name for *_, exts in found for e in exts}
    for agent, module, *_ in found:
        if agent.name in every_ext:
            out = Refusals(module)
            message = (
                f"agent {agent.name!r} has the name of a loaded extension; names share one space"
            )
            out.add("name", "namespace_collision", message)
            refusals += out.items
    every_kind = [qualify(a.name, k.name) for a, *_ in found for k in a.artifacts]
    every_kind += [k for *_, exts in found for e in exts for k in e.kinds]
    refusals += cross_agent_refusals([(a, m) for a, m, *_ in found], every_kind)
    if refusals:
        raise LoadError(tuple(refusals))
    definitions = tuple(_definition(a, m, f, exts) for a, m, f, exts in found)
    if catalogue is not None:
        _register_event_types([e for d in definitions for e in d.extensions], catalogue)
    return definitions


def _load_named(
    names: Iterable[str], search: Sequence[Path], catalogue: Catalogue | None
) -> tuple[tuple[LoadedExtension, ...], list[LoadRefusal]]:
    """Each named extension and the refusals found loading them, in sorted order."""
    loaded: list[LoadedExtension] = []
    refusals: list[LoadRefusal] = []
    for name in sorted(set(names)):
        folder = _find(name, search)
        if folder is None:
            out = Refusals(name)
            out.add("extensions", "extension_not_found", f"no folder {name!r} on the search path")
            refusals += out.items
            continue
        one, problems = _load_extension(name, folder, catalogue)
        refusals += problems
        if one is not None and not problems:
            loaded.append(one)
    return tuple(loaded), refusals


def _register_event_types(extensions: Iterable[LoadedExtension], catalogue: Catalogue) -> None:
    """Register every loaded extension's event types; already checked, so none is refused."""
    for e in extensions:
        for spec in e.event_types:
            catalogue.register_extension(spec, extension=e.ext.name)


def _find(name: str, search: Sequence[Path]) -> Path | None:
    for root in search:
        if (root / name / "__init__.py").is_file():
            return root / name
    return None


def _scan(folder: Path, findings: Iterable[SourceFinding]) -> list[LoadRefusal]:
    """Source findings as refusals, each naming the file it is in."""
    field_of = {"import_outside_api": "imports", "stop_policy_class": "classes"}
    refusals: list[LoadRefusal] = []
    for s in findings:
        out = Refusals(f"{folder.name}/{s.file}")
        out.add(field_of[s.code], s.code, f"line {s.line}: {s.message}")
        refusals += out.items
    return refusals


def _load_extension(
    name: str, folder: Path, catalogue: Catalogue | None
) -> tuple[LoadedExtension | None, list[LoadRefusal]]:
    out = Refusals(f"{name}/__init__.py")
    out.items += _scan(folder, scan_sources(folder))  # an extension imports no other extension
    try:
        value = import_value(folder, f"_aksha_extension_{name}", None, "EXTENSION")
    except Exception as exc:  # any error in the extension's own code is a refusal
        out.add("__init__.py", "extension_import_failed", f"{type(exc).__name__}: {exc}")
        return None, out.items
    if not isinstance(value, Extension):
        out.add("EXTENSION", "no_extension_value", "define EXTENSION = Extension(...)")
        return None, out.items
    if value.name != name:
        out.add("name", "extension_name_mismatch", f"named {value.name!r}, folder {name!r}")
    out.items += api_refusals(out.module, value.requires_api)
    for label in CONTRIBUTIONS:
        names = [local_name(item) for item in getattr(value, label)]
        out.items += name_refusals(out.module, label, names)
    for tool in value.tools:
        if tool.spec.name in LOADER_ADDED_TOOLS:
            out.add("tools", "name_invalid", f"{tool.spec.name!r} is a loader-added tool name")
        full = qualify(name, tool.spec.name)
        out.items += spec_refusals(full, tool, declarer=name, module=out.module)
    event_type_refusals(name, value.event_types, catalogue, out)
    return LoadedExtension(
        ext=value,
        folder=folder,
        file_hashes=file_hashes(folder),
        tools=MappingProxyType({qualify(name, t.spec.name): t for t in value.tools}),
        kinds=MappingProxyType({qualify(name, k.name): k for k in value.artifacts}),
        relations=MappingProxyType({qualify(name, r.name): r for r in value.relations}),
        projections=MappingProxyType({qualify(name, p.name): p for p in value.projections}),
        event_types=tuple(value.event_types),
    ), out.items


def _import_agent(folder: Path, module: str) -> tuple[Agent | None, list[LoadRefusal]]:
    out = Refusals(module)
    findings = scan_sources(folder)
    try:
        value = import_value(folder, f"_aksha_agent_{folder.name}", "agent", "AGENT")
    except Exception as exc:  # any error in the agent's own code is a refusal
        out.items += _scan(folder, findings)
        out.add("agent.py", "agent_import_failed", f"{type(exc).__name__}: {exc}")
        return None, out.items
    named = set(value.extensions) if isinstance(value, Agent) else set()
    kept = [f for f in findings if f.named_extension() not in named]
    out.items += _scan(folder, kept)
    if not isinstance(value, Agent):
        out.add("AGENT", "no_agent_value", "agent.py must define AGENT = Agent(...)")
        return None, out.items
    if value.name != folder.name:
        out.add("name", "agent_name_mismatch", f"named {value.name!r}, folder {folder.name!r}")
        return None, out.items
    return value, out.items


def _inside(folder: Path, rel: str) -> bool:
    return (folder / rel).resolve().is_relative_to(folder.resolve())


def _file_refusals(agent: Agent, folder: Path, module: str) -> list[LoadRefusal]:
    """Prompt files that are missing or outside the folder; source roots inside it (S3)."""
    out = Refusals(module)
    prompts: list[tuple[str | None, str, str]] = [(None, "instructions", agent.instructions)]
    prompts += [(path, "brief", t.brief) for path, t in walk_tasks(agent.tasks) if t.brief]
    for task, label, rel in prompts:
        if not (_inside(folder, rel) and (folder / rel).is_file()):
            out.add(label, "prompt_missing", f"{rel!r} is not a file in the agent folder", task)
    for source in agent.sources:
        if source.root is not None and _inside(folder, source.root):
            message = (
                f"source {source.name!r}: root {source.root!r} is inside the agent folder, so "
                "every edit would change the definition hash; keep fixtures outside"
            )
            out.add("sources", "source_root_inside_agent", message)
    return out.items


def _definition(
    agent: Agent, module: str, folder: Path, exts: tuple[LoadedExtension, ...]
) -> LoadedDefinition:
    out = Refusals(module)
    kinds, relations, projections = own_and_contributed(agent, exts, out)
    assert not out.items, "merge refusals are checked before a definition is built"
    registry = ToolRegistry.build(agent, exts)
    return LoadedDefinition(
        agent=agent,
        module=module,
        folder=folder,
        extensions=exts,
        tools=registry.listed(),
        registry=registry,
        kinds=MappingProxyType(kinds),
        relations=MappingProxyType(relations),
        projections=MappingProxyType(projections),
        model_aliases=MappingProxyType(
            {qualify(agent.name, a.name): a for a in agent.model_aliases}
        ),
        definition_hash=definition_hash(
            folder,
            exts,
            API_VERSION,
            tools_hash=registry.canonical_hash(),
            context_hash=policy_hash(agent.context_policy),
        ),
        file_hashes=file_hashes(folder),
    )
