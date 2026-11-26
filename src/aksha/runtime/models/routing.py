"""Model routing by class: bind each alias to one adapter, and the cascade rules (D2, D3, D5).

Routing is by declaration, not by request content. An agent declares aliases with their
class; every Task also has the implicit generation alias ``generate``. At the start of a
run, the run mode's adapters are bound to the aliases by :func:`bind_aliases`, which lists
every refusal at once (:class:`~aksha.runtime.models.errors.BindRefused`):

1. ``mode_unknown``: the mode is ``fake``, ``scripted``, ``replay`` or ``live``.
2. ``not_an_adapter``: the object has no ``model_class`` and ``capabilities``.
3. ``unbound_alias``: a declared alias (or ``generate``) has no adapter;
   ``unknown_alias``: an adapter is given for an alias nobody declared.
4. ``model_class_mismatch``: the adapter's class is not the alias's class.
5. ``unsupported_type``: the alias declares a field or question type the adapter's
   capability record does not list; ``no_spans``: a cited field needs an adapter that
   returns spans; ``no_distribution``: a classification adapter returns a distribution.
6. ``adapted_not_allowed``: an adapted adapter (a general model under a forced schema,
   classification or extraction, G3) is bound only where the alias sets
   ``allow_adapted`` and does not gate an external effect.
7. ``not_offline``: ``fake``, ``scripted`` and ``replay`` bind offline adapters only.

The binding (alias, class, adapter class, native) is recorded on ``run.started`` with its
hash, and a run never resumes under a different one (``binding_changed``). Configuration
and keys are never recorded.

Fallback never crosses a class (:func:`cascade_for`): a fast-class alias's chain holds
only adapters of its class, natives first; an adapted one only when the alias allows it
and gates no effect; an extraction candidate that returns no spans never stands in for
a cited field (an adapted extractor returns the spans code located its quotes at).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

from aksha.runtime.canonical import sha256_hex
from aksha.runtime.models.errors import BindRefusal, BindRefused
from aksha.runtime.models.ports import (
    Adapter,
    ClassificationModel,
    ExtractionModel,
    GenerationModel,
)
from aksha.runtime.models.types import (
    IMPLICIT_GENERATION_ALIAS,
    OFFLINE_MODES,
    RUN_MODES,
    ModelAlias,
    ModelCapabilities,
    RunMode,
)

BINDING_CHANGED = "binding_changed"
"""Why a run is not resumed under a different model binding."""

LIVE_ENV_PREFIX = "AKSHA_ALIAS_"
"""``AKSHA_ALIAS_<NAME>=module:Class`` names the live adapter of one alias (D9)."""


def adapter_name(adapter: object) -> str:
    """``module:Class`` of an adapter: what the record says answered, never its settings."""
    cls = type(adapter)
    return f"{cls.__module__}:{cls.__qualname__}"


def _capabilities(adapter: object) -> ModelCapabilities | None:
    caps = getattr(adapter, "capabilities", None)
    return caps if isinstance(caps, ModelCapabilities) else None


@dataclass(frozen=True)
class Binding:
    """One alias's binding as ``run.started`` records it."""

    alias: str
    model_class: str
    adapter: str
    native: bool

    def as_json(self) -> dict[str, Any]:
        """The record's JSON form."""
        return {
            "alias": self.alias,
            "model_class": self.model_class,
            "adapter": self.adapter,
            "native": self.native,
        }


@dataclass(frozen=True)
class AliasTable:
    """Each alias of one agent bound to one adapter, for one run mode."""

    mode: RunMode
    aliases: Mapping[str, ModelAlias]
    adapters: Mapping[str, Adapter]

    def bindings(self) -> tuple[Binding, ...]:
        """Every binding, by alias name."""
        out = []
        for name in sorted(self.aliases):
            adapter = self.adapters[name]
            caps = _capabilities(adapter)
            assert caps is not None, "bind_aliases checked every capability record"
            out.append(
                Binding(name, self.aliases[name].model_class, adapter_name(adapter), caps.native)
            )
        return tuple(out)

    def binding_hash(self) -> str:
        """``sha256:`` over the mode and every binding: what a resume must match."""
        record = {"mode": self.mode, "models": [b.as_json() for b in self.bindings()]}
        return "sha256:" + sha256_hex(record)

    def run_started_fields(self) -> dict[str, Any]:
        """``run_mode``, ``models`` and ``model_binding`` for ``run.started`` (D9)."""
        return {
            "run_mode": self.mode,
            "models": [b.as_json() for b in self.bindings()],
            "model_binding": self.binding_hash(),
        }

    def native(self, alias: str) -> bool:
        """Whether ``alias``'s adapter is native."""
        caps = _capabilities(self.adapters[alias])
        return caps is not None and caps.native

    def generation(self, alias: str = IMPLICIT_GENERATION_ALIAS) -> GenerationModel:
        """``alias``'s generation adapter; ``KeyError`` for an alias of another class."""
        return cast(GenerationModel, self._of(alias, "generation"))

    def extraction(self, alias: str) -> ExtractionModel:
        """``alias``'s extraction adapter; ``KeyError`` for an alias of another class."""
        return cast(ExtractionModel, self._of(alias, "extraction"))

    def classification(self, alias: str) -> ClassificationModel:
        """``alias``'s classification adapter; ``KeyError`` for an alias of another class."""
        return cast(ClassificationModel, self._of(alias, "classification"))

    def _of(self, alias: str, model_class: str) -> Adapter:
        declared = self.aliases.get(alias)
        if declared is None or declared.model_class != model_class:
            raise KeyError(f"{alias!r} is not a {model_class} alias")
        return self.adapters[alias]


def declared_aliases(aliases: Iterable[ModelAlias]) -> dict[str, ModelAlias]:
    """The agent's aliases by name, with the implicit ``generate`` when it is not declared."""
    out = {a.name: a for a in aliases}
    out.setdefault(IMPLICIT_GENERATION_ALIAS, ModelAlias(IMPLICIT_GENERATION_ALIAS, "generation"))
    return out


def _fit(alias: ModelAlias, adapter: object, mode: str) -> list[BindRefusal]:
    """Every refusal of binding ``adapter`` to ``alias`` (module doc, items 2 and 4 to 7)."""
    name = alias.name
    caps = _capabilities(adapter)
    cls = getattr(adapter, "model_class", None)
    if caps is None or cls is None:
        message = "the object has no model_class and capability record"
        return [BindRefusal(name, "not_an_adapter", message)]
    if cls != alias.model_class:
        message = f"a {cls} adapter is bound to a {alias.model_class} alias"
        return [BindRefusal(name, "model_class_mismatch", message)]
    out: list[BindRefusal] = []
    unsupported = sorted(
        {f.type for f in alias.fields if f.type not in caps.field_types}
        | {q.type for q in alias.questions if q.type not in caps.question_types}
    )
    if unsupported:
        message = f"the adapter does not support {', '.join(unsupported)}"
        out.append(BindRefusal(name, "unsupported_type", message))
    if any(f.cited for f in alias.fields) and not caps.returns_spans:
        out.append(BindRefusal(name, "no_spans", "a cited field needs an adapter with spans"))
    if alias.model_class == "classification" and not caps.returns_distribution:
        message = "a classification adapter returns a distribution"
        out.append(BindRefusal(name, "no_distribution", message))
    if (
        not caps.native
        and alias.model_class != "generation"
        and (not alias.allow_adapted or alias.gates_effect)
    ):
        message = f"an adapted {alias.model_class} adapter needs allow_adapted and no gated effect"
        out.append(BindRefusal(name, "adapted_not_allowed", message))
    if mode in OFFLINE_MODES and not caps.offline:
        message = f"{mode} mode binds offline adapters only"
        out.append(BindRefusal(name, "not_offline", message))
    return out


def bind_aliases(
    aliases: Iterable[ModelAlias], mode: RunMode, adapters: Mapping[str, object]
) -> AliasTable:
    """Bind every alias (and ``generate``) to its adapter in ``adapters``, keyed by alias.

    Raises ``BindRefused`` listing every refusal of the module doc.
    """
    if mode not in RUN_MODES:
        raise BindRefused((BindRefusal("*", "mode_unknown", f"no run mode {mode!r}"),))
    declared = declared_aliases(aliases)
    refusals: list[BindRefusal] = []
    for name in sorted(declared):
        if name not in adapters:
            refusals.append(BindRefusal(name, "unbound_alias", "no adapter is given"))
        else:
            refusals += _fit(declared[name], adapters[name], mode)
    for name in sorted(set(adapters) - set(declared)):
        refusals.append(BindRefusal(name, "unknown_alias", "no such alias is declared"))
    if refusals:
        raise BindRefused(tuple(refusals))
    bound = {name: cast(Adapter, adapters[name]) for name in declared}
    return AliasTable(mode, MappingProxyType(declared), MappingProxyType(bound))


@dataclass(frozen=True)
class Candidate:
    """One adapter in an alias's cascade, and the alias it is bound to."""

    alias: str
    adapter: Adapter
    native: bool


def _serves(primary: ModelAlias, caps: ModelCapabilities) -> bool:
    """Whether a candidate with ``caps`` can answer what ``primary`` asks."""
    if any(f.type not in caps.field_types for f in primary.fields):
        return False
    if any(q.type not in caps.question_types for q in primary.questions):
        return False
    if any(f.cited for f in primary.fields) and not caps.returns_spans:
        return False
    if primary.model_class == "classification" and not caps.returns_distribution:
        return False
    return caps.native or (primary.allow_adapted and not primary.gates_effect)


def cascade_for(alias: str, table: AliasTable) -> tuple[Candidate, ...]:
    """The ordered candidates ``alias`` tries: itself, then its fallbacks (module doc).

    Pure. Every candidate has the alias's class; natives come before adapted ones.
    """
    primary = table.aliases[alias]
    names = list(dict.fromkeys((alias, *primary.fallback)))
    chain: list[Candidate] = []
    for name in names:
        other = table.aliases.get(name)
        if other is None or other.model_class != primary.model_class:
            continue  # the loader refuses these; never cross a class at run time
        adapter = table.adapters[name]
        caps = _capabilities(adapter)
        if caps is None or not _serves(primary, caps):
            continue
        chain.append(Candidate(name, adapter, caps.native))
    return tuple(sorted(chain, key=lambda c: not c.native))


def binding_refusal(recorded: str | None, table: AliasTable) -> str | None:
    """``binding_changed`` when a run recorded another binding hash (or none); else None."""
    return None if recorded == table.binding_hash() else BINDING_CHANGED


def live_env_name(alias: str) -> str:
    """The variable naming ``alias``'s live adapter: ``AKSHA_ALIAS_CLASSIFY_SUPPORT``."""
    return LIVE_ENV_PREFIX + "".join(c if c.isalnum() else "_" for c in alias).upper()


def live_model_name(alias: str, model_class: str, env: Mapping[str, str]) -> str:
    """The model a live adapter of ``alias`` asks for: per alias, per class, else one for all.

    ``AKSHA_LIVE_MODEL_<ALIAS>`` (``page_facts`` reads ``AKSHA_LIVE_MODEL_PAGE_FACTS``),
    then ``AKSHA_LIVE_MODEL_<CLASS>`` (``GENERATION``, ``EXTRACTION``, ``CLASSIFICATION``),
    then ``AKSHA_LIVE_MODEL``; empty when none is set.
    """
    folded = "".join(c if c.isalnum() else "_" for c in alias).upper()
    return (
        env.get(f"AKSHA_LIVE_MODEL_{folded}")
        or env.get(f"AKSHA_LIVE_MODEL_{model_class.upper()}")
        or env.get("AKSHA_LIVE_MODEL", "")
    )


def import_class(path: str) -> type:
    """The class ``module:Class`` names, imported now. Only live mode calls this (D9)."""
    module, _, name = path.partition(":")
    found = getattr(importlib.import_module(module), name)
    if not isinstance(found, type):
        raise TypeError(f"{path} is not a class")
    return found


def live_adapters(
    aliases: Iterable[ModelAlias],
    env: Mapping[str, str],
    load_class: Callable[[str], type] = import_class,
    defaults: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Each alias's live adapter, named by ``AKSHA_ALIAS_<NAME>`` and built as ``cls(alias, env)``.

    An alias with no variable takes ``defaults[<its model class>]`` when given (the
    wiring passes the built-in live adapter of each class). The adapter reads its own key
    from ``env`` in ``__init__`` and keeps it off every record. Raises ``BindRefused``:
    ``live_adapter_unnamed`` (no variable and no default), ``live_adapter_invalid`` (not
    ``module:Class``, or nothing importable there).
    """
    out: dict[str, object] = {}
    refusals: list[BindRefusal] = []
    declared = declared_aliases(aliases)
    for name in sorted(declared):
        var = live_env_name(name)
        path = env.get(var) or (defaults or {}).get(declared[name].model_class)
        if not path:
            refusals.append(BindRefusal(name, "live_adapter_unnamed", f"set {var}=module:Class"))
            continue
        module, sep, cls_name = path.partition(":")
        if not (module and sep and cls_name):
            refusals.append(BindRefusal(name, "live_adapter_invalid", f"{var} is not module:Class"))
            continue
        try:
            cls = load_class(path)
        except (ImportError, AttributeError, TypeError):
            refusals.append(BindRefusal(name, "live_adapter_invalid", f"{var} names no class"))
            continue
        out[name] = cls(name, env)
    if refusals:
        raise BindRefused(tuple(refusals))
    return out
