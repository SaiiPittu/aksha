"""Validate one agent definition value against the extensions it names (05d 2.7, ADR-P20 D6).

Pure: no file is read here; the loader adds the refusals that need files. The order is
fixed so the first refusal is the most basic one: the API requirement, the agent's name,
its extensions, kinds and schemas, relations, tools (with the tool registry's refusals,
``tools/checks.py``), model aliases, sources, stop-policy
opt-ins, completion checks, then the Task tree.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator, SchemaError

from aksha.runtime.artifacts.types import PROVENANCE_RELATIONS
from aksha.runtime.context.policy import POLICY_INVALID, policy_refusals
from aksha.runtime.definitions.errors import LoadRefusal, Refusals
from aksha.runtime.definitions.names import is_local_name, qualify, resolve_kind
from aksha.runtime.definitions.tree_checks import (
    TreeContext,
    check_tree,
    is_builtin_opt_in,
    is_function,
    is_granted,
    reads_of,
    stop_policy_message,
    walk_tasks,
)
from aksha.runtime.definitions.types import Agent
from aksha.runtime.definitions.versions import API_VERSION, satisfies
from aksha.runtime.invalidation.modes import INVALIDATION_MODES
from aksha.runtime.models.checks import alias_problems
from aksha.runtime.models.types import MODEL_CLASSES
from aksha.runtime.tasks.tree import MAX_CONCURRENCY
from aksha.runtime.tools.checks import tool_refusals
from aksha.runtime.tools.loader_added import LOADER_ADDED_TOOLS
from aksha.runtime.tools.types import ToolDecl, ToolUse

if TYPE_CHECKING:
    from aksha.runtime.definitions.loader import LoadedExtension


def api_refusals(module: str, requires_api: str) -> list[LoadRefusal]:
    """``requires_api_invalid`` or ``api_mismatch`` against ``API_VERSION``, or nothing."""
    out = Refusals(module)
    try:
        if not satisfies(requires_api, API_VERSION):
            message = f"requires_api {requires_api!r} does not accept API_VERSION {API_VERSION}"
            out.add("requires_api", "api_mismatch", message)
    except ValueError as exc:
        out.add("requires_api", "requires_api_invalid", str(exc))
    return out.items


def name_refusals(
    module: str, field: str, names: Iterable[str], *, local: bool = True
) -> list[LoadRefusal]:
    """``name_invalid`` for a malformed name, ``duplicate_name`` for a repeated one."""
    out = Refusals(module)
    seen: set[str] = set()
    for name in names:
        if not name or (local and not is_local_name(name)):
            out.add(field, "name_invalid", f"{name!r} must be a lower-case name with no dot")
        elif name in seen:
            out.add(field, "duplicate_name", f"{name!r} is declared twice")
        seen.add(name)
    return out.items


def validate_agent(
    agent: Agent, *, module: str, extensions: Sequence[LoadedExtension]
) -> list[LoadRefusal]:
    """Every refusal of ``agent``'s declarations, in the fixed order. Empty means it loads."""
    out = Refusals(module)
    out.items += api_refusals(module, agent.requires_api)
    out.items += name_refusals(module, "name", [agent.name])
    named = {e.ext.name: e for e in extensions if e.ext.name in agent.extensions}
    for name in agent.extensions:
        if name not in named:
            out.add("extensions", "unknown_extension", f"no extension named {name!r} was found")
    own_kinds = {qualify(agent.name, k.name) for k in agent.artifacts}
    ext_kinds = {k for e in named.values() for k in e.kinds}
    _kinds(agent, out)
    _relations(agent, out, own_kinds | ext_kinds)
    tool_names, decls = _tools(agent, out, named)
    out.items += tool_refusals(agent, decls, list(named.values()), module=module)
    _aliases(agent, out)
    out.items += name_refusals(module, "sources", [s.name for s in agent.sources])
    for opt in agent.stop_policies:
        if not is_builtin_opt_in(opt):
            out.add("stop_policies", "unknown_stop_policy", stop_policy_message(opt))
    if agent.invalidation not in INVALIDATION_MODES:
        modes = ", ".join(INVALIDATION_MODES)
        message = f"invalidation {agent.invalidation!r} is not a mode; use one of {modes}"
        out.add("invalidation", "invalidation_unknown", message)
    if agent.concurrency != MAX_CONCURRENCY:
        message = f"concurrency {agent.concurrency!r} is not built; v1 runs one instance at a time"
        out.add("concurrency", "concurrency_unsupported", message)
    if agent.context_policy is not None:
        for message in policy_refusals(agent.context_policy, [*tool_names, *LOADER_ADDED_TOOLS]):
            out.add("context_policy", POLICY_INVALID, message)
    if any(not is_function(c) for c in agent.completion):
        out.add("completion", "check_not_callable", "every completion check is a function")
    written = {resolve_kind(w, agent.name) for _, t in walk_tasks(agent.tasks) for w in t.writes}
    derived = {qualify(agent.name, k.name) for k in agent.artifacts if k.derived is not None}
    ctx = TreeContext(
        module=module,
        namespace=agent.name,
        agent_tools=frozenset(tool_names),
        tool_decls=decls,
        own_kinds=frozenset(own_kinds),
        readable_kinds=frozenset(own_kinds | ext_kinds),
        written_or_derived=frozenset(written | derived),
        extension_names=frozenset(named),
        granted=agent.reads,
        aliases=frozenset(a.name for a in agent.model_aliases),
    )
    return out.items + check_tree(agent.tasks, ctx)


def _kinds(agent: Agent, out: Refusals) -> None:
    out.items += name_refusals(out.module, "artifacts", [k.name for k in agent.artifacts])
    for kind in agent.artifacts:
        if not isinstance(kind.schema, type):
            try:
                Draft202012Validator.check_schema(dict(kind.schema))
            except SchemaError as exc:
                out.add("artifacts", "schema_invalid", f"kind {kind.name!r}: {exc.message}")
        if kind.derived is not None and not is_function(kind.derived):
            out.add(
                "artifacts", "derived_not_callable", f"kind {kind.name!r}: derived is a function"
            )
        if any(not is_function(c) for c in kind.readiness):
            out.add(
                "artifacts", "check_not_callable", f"kind {kind.name!r}: readiness is functions"
            )


def _relations(agent: Agent, out: Refusals, known: set[str]) -> None:
    out.items += name_refusals(out.module, "relations", [r.name for r in agent.relations])
    local = {agent.name, *agent.extensions}
    for rel in agent.relations:
        if rel.name in PROVENANCE_RELATIONS:
            message = f"relation {rel.name!r} is lineage, which only the runtime records"
            out.add("relations", "provenance_relation_forbidden", message)
        for end in (*rel.src, *rel.dst):
            full = resolve_kind(end, agent.name)
            if full.startswith("record:"):
                ok = full in agent.reads
            else:
                foreign = full.split(".", 1)[0] not in local
                ok = full in known or (foreign and is_granted(full, agent.reads))
            if not ok:
                message = f"relation {rel.name!r}: {end!r} is not declared or not in reads"
                out.add("relations", "relation_end_unknown", message)


def _tools(
    agent: Agent, out: Refusals, named: Mapping[str, LoadedExtension]
) -> tuple[list[str], dict[str, ToolDecl]]:
    """The tool names Tasks may use, and the declarations behind them."""
    names: list[str] = []
    decls: dict[str, ToolDecl] = {}
    for listed in agent.tools:
        entry: object = listed  # an agent module is not type-checked
        if isinstance(entry, ToolDecl):
            name = entry.spec.name
            if not is_local_name(name) or name in LOADER_ADDED_TOOLS:
                out.add("tools", "name_invalid", f"own tool {name!r} needs a local, free name")
            decls[name] = entry
        elif isinstance(entry, ToolUse):
            name = entry.tool
            ext = named.get(name.split(".", 1)[0]) if "." in name else None
            if ext is not None and name in ext.tools:
                decls[name] = ext.tools[name]
            elif name not in LOADER_ADDED_TOOLS:
                message = f"{name!r} is neither loader-added nor contributed by a named extension"
                out.add("tools", "unknown_tool", message)
            _override_refusals(entry, out)
        else:
            out.add("tools", "unknown_tool", f"{entry!r} is not a ToolUse or ToolDecl")
            continue
        if name in names:
            out.add("tools", "duplicate_tool", f"{name!r} is listed twice")
        names.append(name)
    return names, decls


def _override_refusals(use: ToolUse, out: Refusals) -> None:
    """A ``ToolUse`` override must be a sane number, on a tool the agent may tune."""
    if use.retries is None and use.timeout_ms is None:
        return
    if use.tool in LOADER_ADDED_TOOLS:
        message = f"{use.tool!r} is a runtime tool; its retries and timeout are the runtime's"
        out.add("tools", "override_not_allowed", message)
    if use.retries is not None and (not isinstance(use.retries, int) or use.retries < 0):
        out.add("tools", "override_invalid", f"{use.tool!r}: retries must be 0 or more")
    if use.timeout_ms is not None and (not isinstance(use.timeout_ms, int) or use.timeout_ms < 1):
        out.add("tools", "override_invalid", f"{use.tool!r}: timeout_ms must be 1 or more")


def _aliases(agent: Agent, out: Refusals) -> None:
    aliases = {a.name: a for a in agent.model_aliases}
    names = [a.name for a in agent.model_aliases]
    out.items += name_refusals(out.module, "model_aliases", names, local=False)
    for alias in agent.model_aliases:
        if alias.model_class not in MODEL_CLASSES:
            message = f"{alias.name!r}: no model class {alias.model_class!r}"
            out.add("model_aliases", "alias_class_unknown", message)
            continue
        for code, message in alias_problems(alias):
            out.add("model_aliases", code, message)
        for fb in alias.fallback:
            other = aliases.get(fb)
            if other is None or other.model_class != alias.model_class:
                message = f"{alias.name!r}: fallback {fb!r} is not a {alias.model_class} alias"
                out.add("model_aliases", "alias_fallback_invalid", message)


def cross_agent_refusals(
    agents: Sequence[tuple[Agent, str]], kinds: Iterable[str]
) -> list[LoadRefusal]:
    """Reads and relation ends naming another agent's kind that no loaded agent declares."""
    known = set(kinds)
    refusals: list[LoadRefusal] = []
    for agent, module in agents:
        out = Refusals(module)
        local = {agent.name, *agent.extensions}

        def unknown_foreign(ref: str, namespace: str = agent.name, local: set[str] = local) -> bool:
            full = resolve_kind(ref, namespace)
            foreign = not full.startswith("record:") and full.split(".", 1)[0] not in local
            return foreign and full not in known

        for path, task in walk_tasks(agent.tasks):
            for read in reads_of(task):
                if unknown_foreign(read.kind):
                    out.add("reads", "read_kind_unknown", f"{read.kind!r} is not declared", path)
        for rel in agent.relations:
            for end in (*rel.src, *rel.dst):
                if unknown_foreign(end):
                    message = f"relation {rel.name!r}: {end!r} is not declared by a loaded agent"
                    out.add("relations", "relation_end_unknown", message)
        refusals += out.items
    return refusals
