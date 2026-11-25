"""Load refusals: ``LoadRefusal`` (one broken declaration) and ``LoadError`` (all of them).

Every refusal names module, Task and field, so the message reads like
``notes/agent.py: Task 'collect/write', field 'tools': 'ask' is not in parent 'collect'``.
The first refusal is the most basic one, because the loader checks in a fixed order (D6).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aksha.runtime.errors import AkshaError


@dataclass(frozen=True)
class LoadRefusal:
    """One reason a definition does not load, with a stable snake_case ``code``."""

    module: str
    task: str | None
    field: str
    code: str
    message: str

    def __str__(self) -> str:
        """``module: Task 'path', field 'name': message`` (the Task part only when there is one)."""
        where = f"Task '{self.task}', " if self.task is not None else ""
        return f"{self.module}: {where}field '{self.field}': {self.message}"


class LoadError(AkshaError):
    """The loader refused one or more definitions; nothing was started or written."""

    default_code = "load_refused"

    def __init__(self, refusals: tuple[LoadRefusal, ...]) -> None:
        """Carry every refusal; the error's code is the first refusal's."""
        self.refusals = refusals
        code = refusals[0].code if refusals else self.default_code
        super().__init__(code, "\n".join(str(r) for r in refusals))


@dataclass
class Refusals:
    """Collects one module's refusals in the order they are found.

    ``add`` keeps each refusal to one readable line at the call site:
    ``out.add("tools", "unknown_tool", "...", task="collect")``.
    """

    module: str
    items: list[LoadRefusal] = field(default_factory=list)

    def add(self, field: str, code: str, message: str, task: str | None = None) -> None:
        """Record one refusal of ``field`` (on Task ``task``, if any)."""
        self.items.append(LoadRefusal(self.module, task, field, code, message))
