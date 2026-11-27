"""The compiled Task tree (ADR-P15 D1): every Task by path, its key source and its depth.

``compile_tree`` turns an agent's declared Tasks into a ``CompiledTree`` the scheduler,
the status reducer and completion evaluation read. The loader's own refusals (a leaf with
no check, a tree deeper than four, a ``needs`` naming no sibling, ...) run before it, in
``definitions/tree_checks.py``; ``compile_tree`` refuses again what the v1 scheduler cannot
run, so a hand-built tree in a test meets the same rules:

1. ``keyed_child_unsupported``: a Task with ``per=`` below another keyed Task (v2 LC-8).
   A keyed Task below an un-keyed parent is allowed, so a supervisor's workers fan out
   per item (ADR-P15 D7 amendment); a path still holds at most one keyed Task.
2. ``concurrency_unsupported``: a concurrency setting other than 1 (D5; deterministic
   replay needs one instance at a time, decision 8).
3. ``leaf_without_check``: a leaf Task with ``done=None``.

``state_table`` prints the tree as the README's state table, so the description and the
code cannot drift (Consequences 1).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from aksha.runtime.definitions.names import resolve_kind
from aksha.runtime.tasks.checks import check_name
from aksha.runtime.tasks.errors import TaskUnknown, TreeInvalid
from aksha.runtime.tasks.types import InstanceRef, KeySource, Read, Task

MAX_CONCURRENCY = 1
"""The only concurrency setting v1 accepts; the pick order already returns a tuple."""


@dataclass(frozen=True)
class TaskNode:
    """One declared Task at its path: ``collect/write`` under ``collect``, depth 2."""

    path: str
    task: Task
    depth: int
    parent: str | None

    @property
    def children(self) -> tuple[str, ...]:
        """The paths of the child Tasks, in declaration order."""
        return tuple(f"{self.path}/{c.id}" for c in self.task.children)

    @property
    def is_leaf(self) -> bool:
        """True when the Task declares no child Tasks."""
        return not self.task.children

    @property
    def top(self) -> str:
        """The path of the top-level Task this one sits under (itself at the top)."""
        return self.path.split("/", 1)[0]


@dataclass(frozen=True)
class CompiledTree:
    """An agent's Task tree, compiled: nodes by path, roots in declaration order."""

    agent: str
    roots: tuple[str, ...]
    nodes: Mapping[str, TaskNode]
    concurrency: int = MAX_CONCURRENCY

    def node(self, path: str) -> TaskNode:
        """The node at ``path``, or :class:`TaskUnknown`."""
        try:
            return self.nodes[path]
        except KeyError:
            raise TaskUnknown(message=f"no Task at path {path!r}") from None

    def kind(self, name: str) -> str:
        """A kind's full name as this agent wrote it: ``line`` is ``<agent>.line``."""
        return resolve_kind(name, self.agent)

    def key_node(self, path: str) -> str:
        """The keyed Task at or above ``path`` (its key binds the path), else the top one."""
        found = self.node(path)
        while found.task.per is None and found.parent is not None:
            found = self.nodes[found.parent]
        return found.path

    def key_source(self, path: str) -> KeySource | None:
        """The key source of the keyed Task at or above ``path``; its children share its key."""
        return self.node(self.key_node(path)).task.per

    def ref(self, run: str, path: str, key: str | None) -> InstanceRef:
        """The instance of ``path`` for ``key``, the key dropped when no Task above is keyed."""
        return InstanceRef(run, path, key if self.key_source(path) is not None else None)

    def siblings(self, path: str) -> tuple[str, ...]:
        """The paths of the Tasks beside ``path`` (itself included), in declaration order."""
        parent = self.node(path).parent
        return self.roots if parent is None else self.node(parent).children

    def walk(self) -> Iterator[TaskNode]:
        """Every node, depth first in declaration order."""
        stack = list(reversed(self.roots))
        while stack:
            node = self.nodes[stack.pop()]
            yield node
            stack.extend(reversed(node.children))

    def reads(self, path: str) -> tuple[Read, ...]:
        """The Task's reads with full kind names, a bare string meaning ``keys="own"``."""
        out = []
        for r in self.node(path).task.reads:
            read = r if isinstance(r, Read) else Read(r)
            out.append(Read(self.kind(read.kind), read.keys))
        return tuple(out)


def compile_tree(
    agent: str, tasks: Sequence[Task], *, concurrency: int = MAX_CONCURRENCY
) -> CompiledTree:
    """Compile ``tasks`` (module doc). Raises :class:`TreeInvalid` with every problem."""
    nodes: dict[str, TaskNode] = {}
    problems: list[tuple[str, str, str]] = []
    if concurrency != MAX_CONCURRENCY:
        message = f"concurrency {concurrency} is not built; v1 runs one instance at a time"
        problems.append(("concurrency_unsupported", "", message))

    def keyed_above(parent: str | None) -> bool:
        while parent is not None:
            if nodes[parent].task.per is not None:
                return True
            parent = nodes[parent].parent
        return False

    def add(task: Task, parent: str | None, depth: int) -> None:
        path = task.id if parent is None else f"{parent}/{task.id}"
        if path in nodes:
            problems.append(("duplicate_task", path, f"two sibling Tasks are named {task.id!r}"))
            return
        nodes[path] = TaskNode(path, task, depth, parent)
        if task.per is not None and keyed_above(parent):
            message = "a keyed Task below a keyed Task is not built (keyed_child_unsupported)"
            problems.append(("keyed_child_unsupported", path, message))
        if task.done is None and not task.children:
            problems.append(("leaf_without_check", path, "a leaf Task needs a completion check"))
        for child in task.children:
            add(child, path, depth + 1)

    for task in tasks:
        add(task, None, 1)
    if problems:
        raise TreeInvalid(tuple(problems))
    return CompiledTree(agent, tuple(t.id for t in tasks), MappingProxyType(nodes), concurrency)


def _key_text(source: KeySource | None) -> str:
    if source is None:
        return ""
    if source.origin == "source":
        return f"per record of {source.name}"
    return f"per {source.path} of {source.name}"


def state_table(tree: CompiledTree) -> str:
    """The tree as a markdown table: one row per Task, in pick order (README, L142)."""
    rows = [
        "| Task | Fan-out | Needs | Tools | Writes | Done when |",
        "|---|---|---|---|---|---|",
    ]
    for node in tree.walk():
        t = node.task
        indent = "&nbsp;&nbsp;" * (node.depth - 1)
        done = check_name(t.done) if t.done is not None else "every child is done"
        cells = (
            f"{indent}`{node.path}`",
            _key_text(t.per),
            ", ".join(t.needs),
            ", ".join(t.tools),
            ", ".join(t.writes),
            done,
        )
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"
