"""The loader's refusals of an unsafe tool registry (ADR-P10 D5, S1 to S3). Pure.

Two groups, each refusal naming the tool and a stable code:

1. ``spec_refusals``: what a spec gets wrong on its own. An unknown effect class, the
   ``reserved`` effect class on a tool the runtime did not declare, an owner
   that is not its declarer or ``shared``, a schema that does not compile or is not an
   object schema, a body on a ``model`` or ``reserved`` tool or a missing one elsewhere,
   ``external`` and ``approval`` (S1), ``sandbox``, ``model_alias``, ``result_key`` (S2) and
   ``egress`` (S3) on the wrong effect class or malformed.
2. ``rights_refusals``: what a spec asks for beyond the agent that lists it. Collections
   outside its ``record:`` reads, kinds it may not read or write, an undeclared model
   alias or egress policy. ``tool_only_refusals`` adds a ``tool_only`` kind no tool writes.

The Task-tree refusals (a tool outside the agent's list, a child widening its parent, a
minting tool on a Task that does not extract) are in ``extensions/tree_checks.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jsonschema import Draft202012Validator, SchemaError

from aksha.runtime.definitions.errors import LoadRefusal, Refusals
from aksha.runtime.definitions.names import qualify, resolve_kind
from aksha.runtime.definitions.tree_checks import is_granted
from aksha.runtime.definitions.types import Agent
from aksha.runtime.execution.types import STEP_SEMANTICS
from aksha.runtime.tools.arguments import is_json_pointer
from aksha.runtime.tools.loader_added import RUNTIME_OWNER
from aksha.runtime.tools.types import EFFECT_CLASSES, ToolDecl, ToolSpec

if TYPE_CHECKING:
    from aksha.runtime.definitions.loader import LoadedExtension

SHARED_OWNER = "shared"
"""An owner any declarer may give a tool meant for several agents."""

_NO_BODY = frozenset({"model", "reserved"})
_EGRESS_EFFECTS = frozenset({"read", "external"})


@dataclass(frozen=True)
class ToolRights:
    """What the agent listing a tool may touch, built once per agent by :func:`rights_of`."""

    collections: frozenset[str]
    readable_kinds: frozenset[str]
    writable_kinds: frozenset[str]
    granted: tuple[str, ...]
    aliases: frozenset[str]
    egress: frozenset[str]
    fast_aliases: frozenset[str] = frozenset()


def rights_of(agent: Agent, extensions: Sequence[LoadedExtension]) -> ToolRights:
    """The rights of ``agent`` with the extensions it names."""
    named = [e for e in extensions if e.ext.name in agent.extensions]
    own = {qualify(agent.name, k.name) for k in agent.artifacts}
    contributed = {k for e in named for k in e.kinds}
    return ToolRights(
        collections=frozenset(
            r.removeprefix("record:") for r in agent.reads if r.startswith("record:")
        ),
        readable_kinds=frozenset(own | contributed),
        writable_kinds=frozenset(own | contributed),
        granted=agent.reads,
        aliases=frozenset(a.name for a in agent.model_aliases),
        fast_aliases=frozenset(
            a.name for a in agent.model_aliases if a.model_class in ("extraction", "classification")
        ),
        egress=frozenset(qualify(e.ext.name, p.name) for e in named for p in e.ext.egress),
    )


def spec_refusals(name: str, decl: ToolDecl, *, declarer: str, module: str) -> list[LoadRefusal]:
    """Every refusal of one declared tool on its own; ``declarer`` is its agent or extension."""
    out = Refusals(module)
    spec = decl.spec
    if spec.effect not in EFFECT_CLASSES:
        out.add("tools", "effect_unknown", f"tool {name!r}: no effect class {spec.effect!r}")
        return out.items
    if spec.effect == "reserved" and declarer != RUNTIME_OWNER:
        message = f"tool {name!r}: only the runtime declares a reserved tool (finish, ask)"
        out.add("tools", "reserved_effect_not_allowed", message)
        return out.items
    if spec.owner not in (declarer, SHARED_OWNER):
        message = f"tool {name!r}: owner {spec.owner!r} must be {declarer!r} or {SHARED_OWNER!r}"
        out.add("tools", "tool_owner_invalid", message)
    _schema(name, spec, out)
    if spec.effect in _NO_BODY and decl.body is not None:
        out.add("tools", "body_not_allowed", f"tool {name!r}: a {spec.effect} tool has no body")
    elif spec.effect not in _NO_BODY and decl.body is None:
        out.add("tools", "body_missing", f"tool {name!r}: a {spec.effect} tool needs a body")
    _external(name, spec, out)
    _by_class(name, spec, out)
    _edits(name, spec, out)
    return out.items


_EDIT_EFFECTS = frozenset({"store", "exec"})
"""The effect classes whose bodies may edit a record (decision 38)."""


def _edits(name: str, spec: ToolSpec, out: Refusals) -> None:
    """``edits`` on a store or exec tool, naming a required string argument (decision 38)."""
    edit = spec.edits
    if edit is None:
        return
    if spec.effect not in _EDIT_EFFECTS:
        message = f"tool {name!r}: edits is for store and exec tools, not {spec.effect}"
        out.add("tools", "edits_not_allowed", message)
        return
    if not edit.source:
        out.add("tools", "edits_invalid", f"tool {name!r}: edits names no source")
    parts = edit.record.split("/")[1:] if is_json_pointer(edit.record) else []
    if len(parts) != 1:
        message = f"tool {name!r}: edits.record {edit.record!r} is not a top-level argument"
        out.add("tools", "edits_invalid", message)
        return
    props = spec.schema.get("properties", {})
    arg = props.get(parts[0]) if isinstance(props, Mapping) else None
    required = spec.schema.get("required", ())
    if not isinstance(arg, Mapping) or arg.get("type") != "string" or parts[0] not in required:
        message = f"tool {name!r}: edits.record names {parts[0]!r}, not a required string argument"
        out.add("tools", "edits_invalid", message)


def _schema(name: str, spec: ToolSpec, out: Refusals) -> None:
    try:
        Draft202012Validator.check_schema(dict(spec.schema))
    except SchemaError as exc:
        out.add("tools", "schema_invalid", f"tool {name!r}: {exc.message}")
        return
    if spec.schema.get("type") != "object":
        out.add("tools", "schema_not_object", f'tool {name!r}: the schema needs "type": "object"')


def _external(name: str, spec: ToolSpec, out: Refusals) -> None:
    """S1: ``external`` exactly on external tools; ``approval`` only with ``external``."""
    effect = spec.external
    if spec.effect == "external" and effect is None:
        message = f"tool {name!r}: an external tool declares external=EffectSpec(...)"
        out.add("tools", "external_missing", message)
    elif spec.effect != "external" and effect is not None:
        out.add("tools", "external_not_allowed", f"tool {name!r}: only external tools have one")
    if effect is not None:
        if effect.semantics not in STEP_SEMANTICS:
            message = f"tool {name!r}: semantics {effect.semantics!r} is not one of the three"
            out.add("tools", "semantics_unknown", message)
        elif effect.semantics == "reconcile" and effect.system is None:
            message = f"tool {name!r}: reconcile needs a system of record to ask"
            out.add("tools", "reconcile_without_system", message)
        for field in effect.key_fields:
            if not is_json_pointer(field) or field == "":
                message = f"tool {name!r}: key field {field!r} is not a JSON pointer"
                out.add("tools", "key_field_invalid", message)
        for field in effect.record_fields:
            if not is_json_pointer(field) or field == "":
                message = f"tool {name!r}: record field {field!r} is not a JSON pointer"
                out.add("tools", "record_field_invalid", message)
            elif field not in effect.key_fields:
                # ADR-P29 B3: two records under one idempotency key would make the second
                # write the first one's replay, so record identity is part of request identity.
                message = f"tool {name!r}: record field {field!r} is not one of the key fields"
                out.add("tools", "record_field_not_keyed", message)
    if spec.approval is not None and effect is None:
        message = f"tool {name!r}: approval is for external tools with an effect spec"
        out.add("tools", "approval_without_external", message)


def _by_class(name: str, spec: ToolSpec, out: Refusals) -> None:
    """``sandbox``, ``model_alias``, ``result_key`` and ``egress`` on the right class only."""
    if spec.effect == "exec" and spec.sandbox is None:
        out.add("tools", "exec_without_sandbox", f"tool {name!r}: an exec tool needs sandbox=")
    elif spec.effect != "exec" and spec.sandbox is not None:
        out.add("tools", "sandbox_not_allowed", f"tool {name!r}: only exec tools have sandbox")
    if spec.effect == "model" and spec.model_alias is None:
        out.add("tools", "model_alias_missing", f"tool {name!r}: a model tool names an alias")
    elif spec.effect != "model" and spec.model_alias is not None:
        message = f"tool {name!r}: only model tools have model_alias"
        out.add("tools", "model_alias_not_allowed", message)
    if spec.result_key is not None:
        if not is_json_pointer(spec.result_key):
            message = f"tool {name!r}: result_key {spec.result_key!r} is not a JSON pointer"
            out.add("tools", "result_key_invalid", message)
        elif not spec.recorded_query:
            message = f"tool {name!r}: result_key is for recorded queries"
            out.add("tools", "result_key_without_query", message)
    if spec.egress is not None and spec.effect not in _EGRESS_EFFECTS:
        message = f"tool {name!r}: egress is for read and external tools, not {spec.effect}"
        out.add("tools", "egress_not_allowed", message)


def rights_refusals(
    name: str, decl: ToolDecl, rights: ToolRights, *, declarer: str, module: str
) -> list[LoadRefusal]:
    """Every refusal of a tool's asks beyond the rights of the agent that lists it."""
    out = Refusals(module)
    spec = decl.spec
    for collection in sorted(spec.collections - rights.collections):
        message = f"tool {name!r}: 'record:{collection}' is not in the agent's reads"
        out.add("tools", "collection_not_read", message)
    for kind in sorted(spec.reads_kinds):
        full = resolve_kind(kind, declarer)
        if full not in rights.readable_kinds and not is_granted(full, rights.granted):
            message = f"tool {name!r}: reads {full!r}, which the agent may not read"
            out.add("tools", "read_kind_not_granted", message)
    for kind in sorted(spec.writes_kinds):
        full = resolve_kind(kind, declarer)
        if full not in rights.writable_kinds:
            message = f"tool {name!r}: writes {full!r}, which the agent does not own"
            out.add("tools", "write_kind_not_owned", message)
    if spec.model_alias is not None and spec.model_alias not in rights.aliases:
        message = f"tool {name!r}: model alias {spec.model_alias!r} is not declared"
        out.add("tools", "model_alias_unknown", message)
    for alias in sorted(spec.model_aliases - rights.fast_aliases):
        message = f"tool {name!r}: {alias!r} is not an extraction or classification alias"
        out.add("tools", "model_alias_unknown", message)
    if spec.egress is not None and resolve_kind(spec.egress, declarer) not in rights.egress:
        message = f"tool {name!r}: no egress policy {spec.egress!r} in the named extensions"
        out.add("tools", "egress_policy_unknown", message)
    return out.items


def tool_only_refusals(
    agent: Agent, decls: Mapping[str, ToolDecl], *, module: str
) -> list[LoadRefusal]:
    """A kind declared ``save="tool_only"`` that no listed tool writes."""
    out = Refusals(module)
    written = {
        resolve_kind(kind, declarer_of(name, agent.name))
        for name, decl in decls.items()
        for kind in decl.spec.writes_kinds
    }
    for kind in agent.artifacts:
        if kind.save == "tool_only" and qualify(agent.name, kind.name) not in written:
            message = f"kind {kind.name!r} is saved by a tool only, and no listed tool writes it"
            out.add("artifacts", "tool_only_kind_without_tool", message)
    return out.items


def declarer_of(name: str, agent: str) -> str:
    """Who declared the tool a Task calls ``name``: ``ext`` for ``ext.tool``, else the agent."""
    return name.split(".", 1)[0] if "." in name else agent


def tool_refusals(
    agent: Agent,
    decls: Mapping[str, ToolDecl],
    extensions: Sequence[LoadedExtension],
    *,
    module: str,
) -> list[LoadRefusal]:
    """Every registry refusal for ``agent``'s listed tools (``decls``, keyed as Tasks name them)."""
    rights = rights_of(agent, extensions)
    refusals: list[LoadRefusal] = []
    for name, decl in decls.items():
        declarer = declarer_of(name, agent.name)
        problems = spec_refusals(name, decl, declarer=declarer, module=module)
        refusals += problems or rights_refusals(
            name, decl, rights, declarer=declarer, module=module
        )
    return refusals + tool_only_refusals(agent, decls, module=module)
