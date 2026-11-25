"""The Task tree walk of the loader (05d section 2.7 step 6), one refusal per broken field.

Depth first, in declaration order: sibling ids and ``needs`` first, then each Task's
tools, reads, writes, key source, completion check, stop-policy opt-ins and model aliases,
then its children. A child's tools narrow its parent's; a top-level Task's narrow the
agent's list.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from aksha.runtime.definitions.errors import LoadRefusal, Refusals
from aksha.runtime.definitions.names import resolve_kind
from aksha.runtime.execution.types import is_reserved_segment
from aksha.runtime.models.types import IMPLICIT_GENERATION_ALIAS
from aksha.runtime.stop_policy.types import BUILTIN_STOP_POLICIES, StopPolicyOptIn
from aksha.runtime.tasks.types import FromField, KeySource, Read, Task
from aksha.runtime.tools.types import ToolDecl

MAX_TASK_DEPTH = 4
"""The deepest a Task may sit; a top-level Task is depth 1."""


@dataclass(frozen=True)
class TreeContext:
    """What the walk checks names against, built once per agent by ``validate_agent``."""

    module: str
    namespace: str
    agent_tools: frozenset[str]
    tool_decls: Mapping[str, ToolDecl]
    own_kinds: frozenset[str]
    readable_kinds: frozenset[str]
    written_or_derived: frozenset[str]
    extension_names: frozenset[str]
    granted: tuple[str, ...]
    aliases: frozenset[str]


def is_function(value: object) -> bool:
    """True when ``value`` can be called. Agent modules are not type-checked, so the loader asks."""
    return callable(value)


def walk_tasks(tasks: Sequence[Task], prefix: str = "") -> Iterator[tuple[str, Task]]:
    """Every Task with its path (``collect/write``), depth first in declaration order."""
    for task in tasks:
        path = f"{prefix}/{task.id}" if prefix else task.id
        yield path, task
        yield from walk_tasks(task.children, path)


def reads_of(task: Task) -> tuple[Read, ...]:
    """The Task's reads, a bare string meaning ``Read(kind, keys="own")``."""
    return tuple(r if isinstance(r, Read) else Read(r) for r in task.reads)


def is_granted(full: str, granted: Sequence[str]) -> bool:
    """True when ``Agent.reads`` lists ``full`` or its namespace as ``ns.*``."""
    namespace = full.split(".", 1)[0]
    return full in granted or f"{namespace}.*" in granted


def check_tree(tasks: Sequence[Task], ctx: TreeContext) -> list[LoadRefusal]:
    """Every refusal in the tree ``tasks``, in walk order."""
    out = Refusals(ctx.module)
    _siblings(tasks, ctx, out, parent=None, prefix="", depth=1)
    return out.items


def _siblings(
    tasks: Sequence[Task],
    ctx: TreeContext,
    out: Refusals,
    *,
    parent: Task | None,
    prefix: str,
    depth: int,
) -> None:
    field = "children" if parent else "tasks"
    ids = [t.id for t in tasks]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        out.add(field, "duplicate_task", f"two sibling Tasks are named {dup!r}", prefix or None)
    _needs(tasks, out, prefix)
    for task in tasks:
        path = f"{prefix}/{task.id}" if prefix else task.id
        if is_reserved_segment(task.id):
            message = f"{task.id!r} is a reserved step-ID segment (t03, c02, k01, w043, end, ...)"
            out.add("id", "task_id_reserved", message, path)
        if depth > MAX_TASK_DEPTH:
            out.add("children", "tree_too_deep", f"Tasks nest at most {MAX_TASK_DEPTH} deep", path)
            continue
        _tools(task, path, parent, ctx, out)
        _reads(task, path, ctx, out)
        _rest(task, path, ctx, out)
        _siblings(task.children, ctx, out, parent=task, prefix=path, depth=depth + 1)


def _needs(tasks: Sequence[Task], out: Refusals, prefix: str) -> None:
    ids = {t.id for t in tasks}
    graph = {t.id: tuple(n for n in t.needs if n in ids) for t in tasks}
    for task in tasks:
        path = f"{prefix}/{task.id}" if prefix else task.id
        for need in task.needs:
            if need not in ids:
                out.add("needs", "needs_unknown", f"{need!r} is not a sibling Task", path)
        if _reaches(graph, task.id, task.id):
            out.add("needs", "needs_cycle", f"{task.id!r} needs itself through siblings", path)
            break  # one refusal per cycle


def _reaches(graph: Mapping[str, tuple[str, ...]], start: str, goal: str) -> bool:
    seen: set[str] = set()
    stack = list(graph.get(start, ()))
    while stack:
        node = stack.pop()
        if node == goal:
            return True
        if node not in seen:
            seen.add(node)
            stack.extend(graph.get(node, ()))
    return False


def _tools(task: Task, path: str, parent: Task | None, ctx: TreeContext, out: Refusals) -> None:
    for name in task.tools:
        if name == "finish" or (name == "ask" and task.interrupt is not None):
            continue  # implicit tools
        if parent is not None and name not in parent.tools:
            out.add("tools", "tool_widens_parent", f"{name!r} is not in parent {parent.id!r}", path)
            continue
        if parent is None and name not in ctx.agent_tools:
            out.add("tools", "tool_not_in_agent", f"{name!r} is not in the agent's tools", path)
            continue
        decl = ctx.tool_decls.get(name)
        if decl is not None and decl.spec.mints_citation_spans and not task.extracts:
            message = f"{name!r} mints citation spans; only Tasks with extracts=True may hold it"
            out.add("tools", "minting_tool_not_extracting", message, path)


def _reads(task: Task, path: str, ctx: TreeContext, out: Refusals) -> None:
    reads = reads_of(task)
    own_keyed = {resolve_kind(r.kind, ctx.namespace) for r in reads if r.keys == "own"}
    for read in reads:
        code, message = _read_refusal(resolve_kind(read.kind, ctx.namespace), ctx)
        if code:
            out.add("reads", code, message, path)
        if isinstance(read.keys, FromField):
            source = resolve_kind(read.keys.kind, ctx.namespace)
            if source not in own_keyed:
                message = f"from_field({source!r}) needs Read({source!r}) on the same Task"
                out.add("reads", "from_field_unread", message, path)


def _read_refusal(full: str, ctx: TreeContext) -> tuple[str, str]:
    """The (code, message) refusing one read, or ("", "") when it is allowed here.

    A read of another agent's kind is allowed here when ``Agent.reads`` grants it; that the
    kind exists is checked once every agent is loaded (``cross_agent_refusals``).
    """
    if full.startswith("record:"):
        if full in ctx.granted:
            return "", ""
        return "read_not_declared", f"{full!r} is not in the agent's reads"
    namespace = full.split(".", 1)[0]
    if namespace == ctx.namespace:
        if full not in ctx.own_kinds:
            return "read_kind_unknown", f"{full!r} is not declared"
        if full not in ctx.written_or_derived:
            return "read_never_written", f"no Task writes {full!r} and it is not derived"
        return "", ""
    if namespace in ctx.extension_names:
        if full in ctx.readable_kinds:
            return "", ""
        return "read_kind_unknown", f"{full!r} is not declared"
    if not is_granted(full, ctx.granted):
        return "read_not_declared", f"{full!r} belongs to another agent and is not in reads"
    return "", ""


def _rest(task: Task, path: str, ctx: TreeContext, out: Refusals) -> None:
    """Writes, key source, completion check, stop-policy opt-ins and model aliases."""
    for kind in task.writes:
        if resolve_kind(kind, ctx.namespace) not in ctx.own_kinds:
            out.add("writes", "write_kind_unknown", f"{kind!r} is not declared here", path)
    per: object = task.per  # an agent module is not type-checked, so check the type here
    if per is not None and not isinstance(per, KeySource):
        message = "expected source_keys(...) or artifact_keys(...)"
        out.add("per", "per_not_key_source", message, path)
    elif isinstance(per, KeySource) and not _key_source_ok(per, ctx):
        out.add("per", "per_kind_unknown", f"{per.name!r} is not a known kind with a path", path)
    if task.done is not None and not is_function(task.done):
        out.add("done", "check_not_callable", "a completion check must be a function", path)
    if task.done is None and not task.children:
        out.add("done", "leaf_without_check", "a leaf Task needs a completion check", path)
    for opt in task.stop_policies:
        if not is_builtin_opt_in(opt):
            out.add("stop_policies", "unknown_stop_policy", stop_policy_message(opt), path)
    for alias in task.model_aliases:
        if alias != IMPLICIT_GENERATION_ALIAS and alias not in ctx.aliases:
            out.add("model_aliases", "unknown_model_alias", f"{alias!r} is not declared", path)


def _key_source_ok(per: KeySource, ctx: TreeContext) -> bool:
    """A source origin is checked later, against the sources; an artifact origin needs a kind."""
    if per.origin != "artifact":
        return True
    return resolve_kind(per.name, ctx.namespace) in ctx.readable_kinds and bool(per.path)


def is_builtin_opt_in(opt: object) -> bool:
    """True for a ``StopPolicyOptIn`` naming one of the built-in stop policies."""
    return isinstance(opt, StopPolicyOptIn) and opt.name in BUILTIN_STOP_POLICIES


def stop_policy_message(opt: object) -> str:
    """Why ``opt`` is refused, listing the built-in names."""
    name = getattr(opt, "name", opt)
    return f"{name!r} is not a built-in stop policy ({', '.join(sorted(BUILTIN_STOP_POLICIES))})"
