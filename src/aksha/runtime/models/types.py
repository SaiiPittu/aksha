"""Model classes and aliases: ``ModelClass``, ``ModelAlias`` and ``ModelAdapterSpec`` (ADR-P07).

An agent declares aliases, each with one model class; a Task names the aliases it uses,
and every Task also has the implicit generation alias ``generate``. At load the run mode
binds each alias to an adapter of the same class. The abstention band and
``ConfidenceThresholds`` arrive with the model interface.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

ModelClass = Literal["extraction", "classification", "generation"]
OnExhausted = Literal["unknown", "review", "fail"]

MODEL_CLASSES: frozenset[str] = frozenset({"extraction", "classification", "generation"})
"""The three model classes."""

IMPLICIT_GENERATION_ALIAS = "generate"
"""The generation alias every Task has without declaring it."""


@dataclass(frozen=True)
class ModelAlias:
    """A named use of a model class; namespaced by the agent at load (``ns.alias``)."""

    name: str
    model_class: ModelClass
    fallback: tuple[str, ...] = ()
    allow_adapted: bool = False
    on_exhausted: OnExhausted | None = None


@dataclass(frozen=True)
class ModelAdapterSpec:
    """A model adapter an extension contributes: its name, class and a factory for it.

    The factory's argument and return types are fixed when the model interface is built.
    """

    name: str
    model_class: ModelClass
    factory: Callable[..., object]
