"""Interrupt values: what a Task declares, and the approval rule a tool declares (ADR-P17).

Built so far: ``DeclaredInterrupt``, ``ApprovalFacts`` and ``ApprovalRule``.
``InterruptView``, ``ActionBody`` and ``ApprovalView`` arrive with the interrupt primitive.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class DeclaredInterrupt:
    """An interrupt a Task declares; it adds the implicit ``ask`` tool.

    ``label`` is the agent's word for it, rendered to people and never branched on.
    """

    kind: Literal["input", "choice"]
    label: str


ApprovalFacts = Callable[[Any, Mapping[str, object]], Mapping[str, str]]
"""Pure: ``(TaskView, args) -> facts`` shown to the person and signed (``Any`` until P15)."""


@dataclass(frozen=True)
class ApprovalRule:
    """Declared on a tool (``ToolSpec.approval``): the call needs a person's approval first.

    ``facts`` builds what the person approves; ``targets`` lists the artifact refs the
    action acts on. ``targets`` is None when the action acts on none (the ADR's default
    lambda, written as None so the declared value has a stable form).
    """

    facts: ApprovalFacts
    targets: Callable[[Mapping[str, object]], tuple[str, ...]] | None = None
