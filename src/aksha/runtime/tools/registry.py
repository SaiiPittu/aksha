"""``ToolRegistry``: the frozen, per-run table of every tool an agent may call (ADR-P10).

``build`` resolves the agent's tool list (own ``ToolDecl``s by local name, ``ToolUse``s of
extension tools as ``ext.tool``, with a ``ToolUse``'s ``retries`` and ``timeout_ms`` written
into the spec's ``RetryPolicy``), adds the three loader-added tools, refuses an unsafe registry
with ``LoadError``, then freezes a copy of each schema and compiles it once. That one copy
is what ``offered`` shows the model and what ``check_arguments`` validates against, so the
two cannot drift (D3). ``canonical_hash`` enters the agent definition version hash (D4).

The registry is built from the agent definition, never from the event store, so nothing
the model writes can change it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from aksha.runtime.canonical import freeze, sha256_hex, thaw
from aksha.runtime.definitions.errors import LoadError, LoadRefusal
from aksha.runtime.definitions.types import Agent
from aksha.runtime.errors import AkshaError
from aksha.runtime.tasks.types import Task
from aksha.runtime.tools.arguments import schema_refusal
from aksha.runtime.tools.checks import tool_refusals
from aksha.runtime.tools.loader_added import LOADER_ADDED_DECLS
from aksha.runtime.tools.types import Err, ToolDecl, ToolUse, ToolView

if TYPE_CHECKING:
    from aksha.runtime.definitions.loader import LoadedExtension


class UnknownTool(AkshaError):
    """No tool of that name is in the registry."""

    default_code = "unknown_tool"


class ToolRegistry:
    """An agent's tools, keyed by the name a Task and the model use. Build it with ``build``."""

    def __init__(
        self,
        agent: Agent,
        listed: Mapping[str, ToolDecl],
        extensions: Sequence[LoadedExtension],
    ) -> None:
        """Hold ``listed`` (the agent's own and extension tools) plus the reserved tools."""
        self._agent = agent
        self._extensions = tuple(extensions)
        self._listed = MappingProxyType(dict(listed))
        decls = {**listed, **LOADER_ADDED_DECLS}
        self._decls: Mapping[str, ToolDecl] = MappingProxyType(dict(sorted(decls.items())))
        self._schemas = {name: thaw(d.spec.schema) for name, d in self._decls.items()}
        self._validators = {name: Draft202012Validator(s) for name, s in self._schemas.items()}

    @classmethod
    def build(cls, agent: Agent, extensions: Sequence[LoadedExtension]) -> ToolRegistry:
        """The registry of ``agent`` with the extensions it names; ``LoadError`` if unsafe."""
        contributed = {n: d for e in extensions for n, d in e.tools.items()}
        listed: dict[str, ToolDecl] = {}
        for entry in agent.tools:
            if isinstance(entry, ToolDecl):
                listed[entry.spec.name] = entry
            elif isinstance(entry, ToolUse) and entry.tool in contributed:
                listed[entry.tool] = with_overrides(contributed[entry.tool], entry)
        registry = cls(agent, listed, extensions)
        refusals = registry.validate()
        if refusals:
            raise LoadError(tuple(refusals))
        return registry

    def listed(self) -> Mapping[str, ToolDecl]:
        """The agent's own and contributed tools, as Tasks name them; loader-added ones aside."""
        return self._listed

    def names(self) -> tuple[str, ...]:
        """Every tool name, sorted, loader-added tools included."""
        return tuple(self._decls)

    def get(self, name: str) -> ToolDecl:
        """The declaration of ``name``; raises ``UnknownTool``."""
        try:
            return self._decls[name]
        except KeyError:
            raise UnknownTool(message=f"no tool named {name!r}") from None

    def is_reserved(self, name: str) -> bool:
        """Whether ``name`` has the ``reserved`` effect class (``finish``, ``ask``)."""
        return name in self._decls and self._decls[name].spec.effect == "reserved"

    def view(self, name: str) -> ToolView:
        """``name`` as the model is shown it, with the registry's frozen copy of the schema."""
        spec = self.get(name).spec
        return ToolView(name=name, description=spec.description, schema=freeze(self._schemas[name]))

    def validator(self, name: str) -> Draft202012Validator:
        """The compiled validator of ``name``'s published schema; raises ``UnknownTool``."""
        self.get(name)
        return self._validators[name]

    def check_arguments(self, name: str, args: Mapping[str, Any]) -> Err | None:
        """None when ``args`` match ``name``'s published schema, else a ``schema`` error."""
        return schema_refusal(self.validator(name), args)

    def offered(self, task: Task, narrowing: frozenset[str] | None = None) -> tuple[ToolView, ...]:
        """What the model is shown on ``task`` this turn, in name order.

        The Task's list plus ``finish`` (and ``ask`` with a declared interrupt). A stop-policy
        narrowing (ADR-P16 ``Prep.tools``) removes tools and never adds one; only tools of
        the ``reserved`` effect class (``finish``, ``ask``) are never narrowed away.
        """
        names = set(task.tools) | {"finish"}
        if task.interrupt is not None:
            names.add("ask")
        if narrowing is not None:
            names = {n for n in names if n in narrowing or self.is_reserved(n)}
        return tuple(self.view(n) for n in sorted(names))

    def validate(self) -> list[LoadRefusal]:
        """The loader refusals of the agent's listed tools (``tools/checks.py``)."""
        module = f"{self._agent.name}/agent.py"
        return tool_refusals(self._agent, self._listed, self._extensions, module=module)

    def canonical(self) -> list[dict[str, Any]]:
        """Every spec as plain JSON data, by name. Bodies are left out: file hashes cover them."""
        out: list[dict[str, Any]] = []
        for name, decl in self._decls.items():
            spec = _plain(decl.spec)
            if spec.get("edits") is None:  # decision 38: a tool that edits nothing hashes as before
                spec.pop("edits", None)
            spec["schema"] = self._schemas[name]
            out.append({"name": name, "spec": spec, "has_body": decl.body is not None})
        return out

    def canonical_hash(self) -> str:
        """``sha256:<hex>`` over :meth:`canonical`; part of the agent definition version hash."""
        return "sha256:" + sha256_hex(self.canonical())


def with_overrides(decl: ToolDecl, use: ToolUse) -> ToolDecl:
    """``decl`` with ``use``'s ``retries`` and ``timeout_ms`` in its ``RetryPolicy``.

    The overridden policy is part of the spec, so it enters :meth:`ToolRegistry.canonical_hash`
    and the agent definition hash. Without overrides ``decl`` is returned as is.
    """
    retry = decl.spec.retry
    if use.retries is not None:
        retry = dataclasses.replace(retry, max_retries=use.retries)
    if use.timeout_ms is not None:
        retry = dataclasses.replace(retry, timeout_ms=use.timeout_ms)
    if retry == decl.spec.retry:
        return decl
    return dataclasses.replace(decl, spec=dataclasses.replace(decl.spec, retry=retry))


def _plain(value: Any) -> Any:
    """A declared value as JSON data: sets sorted, callables reduced to ``True`` (declared)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, frozenset | set):
        return sorted(_plain(v) for v in value)
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    if callable(value):
        return True
    return value
