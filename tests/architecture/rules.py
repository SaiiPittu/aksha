"""The dependency rule and layout rules, as plain functions over a source tree.

The tests call them on the real package and on planted violations in a temporary copy,
so each guard is seen to fail before it is trusted (ADR-P20 D8).
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

RUNTIME_THIRD_PARTY = frozenset({"jsonschema"})
"""The only non-standard-library imports the runtime may use."""


@dataclass(frozen=True)
class Violation:
    """One broken rule: which module, what it imported or defined, and why that is refused."""

    module: str
    target: str
    rule: str

    def __str__(self) -> str:
        return f"{self.module} -> {self.target}: {self.rule}"


def module_name(root: Path, path: Path) -> str:
    """Dotted module name of ``path`` relative to the folder that holds the ``aksha`` package."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(root: Path, path: Path, modules: frozenset[str] = frozenset()) -> list[str]:
    """Every module ``path`` imports, absolute, including imports under ``TYPE_CHECKING``.

    ``from a.b import c`` counts as importing ``a.b.c`` when that is one of ``modules``,
    and as importing ``a.b`` otherwise (``c`` is then a name defined in ``a.b``).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    me = module_name(root, path)
    package = me if path.name == "__init__.py" else me.rpartition(".")[0]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                prefix = ".".join(base)
                mod = f"{prefix}.{node.module}" if node.module else prefix
            else:
                mod = node.module or ""
            for alias in node.names:
                sub = f"{mod}.{alias.name}"
                found.append(sub if sub in modules else mod)
    return found


def _within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _family(name: str, layer: str) -> str | None:
    """``aksha.adapters.storage.sqlite`` -> ``aksha.adapters.storage``; None at the layer root."""
    parts = name.split(".")
    depth = len(layer.split("."))
    return ".".join(parts[: depth + 1]) if len(parts) > depth else None


def check_dependency_rule(root: Path) -> list[Violation]:
    """Check every module under ``root/aksha`` against the dependency rule.

    1. ``aksha.runtime`` imports only itself, the standard library and ``jsonschema``.
    2. ``aksha.adapters.<family>`` imports the runtime and its own family only.
    3. ``aksha.extensions.<name>`` imports ``aksha.runtime.api`` and itself only.
    4. ``aksha.agents.<name>`` imports ``aksha.runtime.api``, extensions and itself only.
    5. ``aksha.interfaces`` imports the runtime, ``aksha.app`` and itself, never an adapter,
       an agent or an extension directly.
    6. ``aksha.app`` (the composition root) and ``aksha`` itself may import anything.
    """
    files = sorted((root / "aksha").rglob("*.py"))
    modules = frozenset(module_name(root, f) for f in files)
    stdlib = sys.stdlib_module_names
    out: list[Violation] = []
    for f in files:
        me = module_name(root, f)
        for target in imports_of(root, f, modules):
            top = target.split(".")[0]
            if top != "aksha":
                third_party = top not in stdlib and top not in RUNTIME_THIRD_PARTY
                if _within(me, "aksha.runtime") and third_party:
                    rule = "runtime imports the standard library and jsonschema only"
                    out.append(Violation(me, target, rule))
                continue
            if target not in modules or target == "aksha":
                continue
            refusal = _refusal(me, target)
            if refusal:
                out.append(Violation(me, target, refusal))
    return out


def _refusal(me: str, target: str) -> str | None:
    """The rule ``me`` breaks by importing the internal module ``target``, or None."""
    if _within(me, "aksha.runtime"):
        return None if _within(target, "aksha.runtime") else "runtime imports no outer layer"
    if _within(me, "aksha.adapters"):
        own = _family(me, "aksha.adapters")
        if _within(target, "aksha.runtime") or target == "aksha.adapters":
            return None
        if own and _within(target, own):
            return None
        return "adapters import runtime ports and their own family only"
    if _within(me, "aksha.extensions"):
        own = _family(me, "aksha.extensions")
        if _within(target, "aksha.runtime.api") or target == "aksha.extensions":
            return None
        if own and _within(target, own):
            return None
        return "extensions import aksha.runtime.api only"
    if _within(me, "aksha.agents"):
        own = _family(me, "aksha.agents")
        if _within(target, "aksha.runtime.api") or _within(target, "aksha.extensions"):
            return None
        if target == "aksha.agents" or (own and _within(target, own)):
            return None
        return "agents import aksha.runtime.api and named extensions only"
    if _within(me, "aksha.interfaces"):
        allowed = ("aksha.runtime", "aksha.app", "aksha.interfaces")
        if any(_within(target, a) for a in allowed):
            return None
        return "interfaces reach adapters, agents and extensions through aksha.app only"
    return None  # aksha.app and aksha/__init__.py: the composition root


def protocols_outside_ports(root: Path) -> list[Violation]:
    """Every ``typing.Protocol`` class under ``aksha/`` defined outside a ``ports.py``."""
    out: list[Violation] = []
    for f in sorted((root / "aksha").rglob("*.py")):
        if f.name == "ports.py":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(_is_protocol(b) for b in node.bases):
                out.append(Violation(module_name(root, f), node.name, "Protocols live in ports.py"))
    return out


def _is_protocol(base: ast.expr) -> bool:
    if isinstance(base, ast.Subscript):
        base = base.value
    if isinstance(base, ast.Name):
        return base.id == "Protocol"
    return isinstance(base, ast.Attribute) and base.attr == "Protocol"


SOURCE_DIR = "src"
"""The repository folder that holds the ``aksha`` package (the src layout)."""


def modules_missing_from_doc(root: Path, doc: Path) -> list[str]:
    """Repository paths of modules under ``root/aksha`` that the structure document omits.

    ``root`` is the source root. The document's tree is rooted at ``src/aksha/``, so each
    module is compared as ``src/aksha/...``, the path a reader sees in the repository.
    """
    listed = tree_paths(doc.read_text(encoding="utf-8"))
    files = sorted((root / "aksha").rglob("*.py"))
    paths = [f"{SOURCE_DIR}/{f.relative_to(root).as_posix()}" for f in files]
    return [p for p in paths if p not in listed]


def tree_paths(markdown: str) -> set[str]:
    """Rebuild file paths from the first ``text`` fenced tree in ``markdown``.

    Each tree line is ``<indent><├── |└── ><name>  <description>``, four columns per level.
    """
    lines = markdown.split("```text\n", 1)[1].split("```", 1)[0].splitlines()
    stack: list[str] = []
    paths: set[str] = set()
    for line in lines:
        marker = max(line.find("├── "), line.find("└── "))
        if marker < 0:
            name = line.strip().split()[0] if line.strip() else ""
            if name.endswith("/"):
                stack = [name.rstrip("/")]
            continue
        depth = marker // 4 + 1
        name = line[marker + 4 :].split()[0]
        stack = stack[:depth]
        if name.endswith("/"):
            stack.append(name.rstrip("/"))
        else:
            paths.add("/".join([*stack, name]))
    return paths


IMPURE_MODULES = frozenset(
    {"time", "random", "secrets", "uuid", "os", "io", "pathlib", "socket", "subprocess",
     "sqlite3", "threading", "asyncio", "urllib", "http", "shutil", "tempfile"}
)  # fmt: skip
"""Modules a module that defines a reducer may not import: clocks, randomness and I/O."""

IMPURE_CALLS = frozenset(
    {"open", "print", "input", "now", "utcnow", "today", "sleep", "mono_ms", "read",
     "read_artifacts", "last_seq", "by_dedup_key", "subscribe", "append", "execute"}
)  # fmt: skip
"""Names a reducer's methods may not call: the clock, the store and I/O (``append`` covers
``store.append``; reducers build new tuples instead of appending to lists)."""


def impure_reducers(root: Path) -> list[Violation]:
    """Every reducer under ``aksha/runtime`` that touches a clock, randomness, I/O or the store.

    A reducer is any class with both an ``initial`` and an ``apply`` method (ADR-P01 D3).
    """
    out: list[Violation] = []
    for f in sorted((root / "aksha" / "runtime").rglob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        me = module_name(root, f)
        reducers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
            and {"initial", "apply"}
            <= {n.name for n in node.body if isinstance(n, ast.FunctionDef)}
        ]
        if not reducers:
            continue
        for target in imports_of(root, f):
            if target.split(".")[0] in IMPURE_MODULES:
                out.append(Violation(me, target, "a reducer's module imports no clock or I/O"))
        for cls in reducers:
            for node in ast.walk(cls):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if name in IMPURE_CALLS:
                    out.append(Violation(me, f"{cls.name}.{name}", "reducers are pure"))
    return out


def agent_terms_in(root: Path, terms: tuple[str, ...]) -> list[Violation]:
    """Every whole-word, case-insensitive use of an agent term in ``root/aksha/runtime``.

    ``terms`` come from ``tests/fixtures/agent_terms.txt``. Comments and docstrings count:
    a reader who sees an agent's word in the runtime assumes the runtime knows that agent.
    """
    import re

    if not terms:
        return []
    pattern = re.compile(r"\b(" + "|".join(re.escape(t) for t in terms) + r")\b", re.IGNORECASE)
    out: list[Violation] = []
    for f in sorted((root / "aksha" / "runtime").rglob("*.py")):
        for match in pattern.finditer(f.read_text(encoding="utf-8")):
            out.append(
                Violation(module_name(root, f), match.group(1), "no agent word in the runtime")
            )
    return out


# --- ADR-P11: the policy gate ------------------------------------------------------------

GATE_MODULES = (
    "gate.py", "rules.py", "paths.py", "permissions.py", "types.py", "matrix.py", "events.py",
)  # fmt: skip
"""Every policy module: all of them are pure, and none appends (the step executor does)."""


def policy_writers(root: Path) -> list[Violation]:
    """Every policy module that imports a storage module, so could reach ``EventStore.append``.

    ADR-P11 D9 says the step executor records each decision; ``step.started`` and
    ``step.completed`` name ``execution`` as their writer (ADR-P01 D4). The policy gate
    returns ``decision_event(...)`` and never holds a store.
    """
    out: list[Violation] = []
    files = sorted((root / "aksha").rglob("*.py"))
    modules = frozenset(module_name(root, f) for f in files)
    for f in sorted((root / "aksha" / "runtime" / "policy").rglob("*.py")):
        for target in imports_of(root, f, modules):
            if _within(target, "aksha.runtime.storage") or _within(target, "aksha.adapters"):
                out.append(Violation(module_name(root, f), target, "the policy gate never appends"))
    return out


def impure_gate(root: Path) -> list[Violation]:
    """Every clock, randomness, I/O or environment use in the deciding policy modules.

    ``check`` is pure (ADR-P11 D1): it reads no clock, no store, no network and no
    environment variable, so no setting outside the agent definition can change a decision.
    """
    out: list[Violation] = []
    folder = root / "aksha" / "runtime" / "policy"
    for name in GATE_MODULES:
        f = folder / name
        if not f.exists():
            continue
        me = module_name(root, f)
        for target in imports_of(root, f):
            if target.split(".")[0] in IMPURE_MODULES:
                out.append(Violation(me, target, "the gate imports no clock, I/O or environment"))
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"), filename=str(f))):
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if called in IMPURE_CALLS or called == "getenv":
                    out.append(Violation(me, str(called), "the gate calls no clock, store or I/O"))
    return out


# --- ADR-P16 D1: stop policies are pure ------------------------------------------------

STOP_POLICY_WRITER = "commit.py"
"""The one stop-policy module that holds a store; every other one decides and is pure."""

PURE_STOP_CALLS = frozenset(
    {"open", "print", "input", "now", "utcnow", "today", "sleep", "mono_ms", "getenv"}
)  # fmt: skip
"""Calls a deciding stop-policy module may not make: the clock and I/O."""


def impure_stop_policies(root: Path) -> list[Violation]:
    """Every clock, randomness, I/O or store use in the deciding stop-policy modules.

    A stop policy reads only the ``StopView`` and the candidate (ADR-P16 D1), so two runs
    over the same log decide the same; only ``commit.py`` reaches the event store.
    """
    out: list[Violation] = []
    files = sorted((root / "aksha").rglob("*.py"))
    modules = frozenset(module_name(root, f) for f in files)
    for f in sorted((root / "aksha" / "runtime" / "stop_policy").rglob("*.py")):
        if f.name == STOP_POLICY_WRITER:
            continue
        me = module_name(root, f)
        for target in imports_of(root, f, modules):
            if target.split(".")[0] in IMPURE_MODULES:
                out.append(Violation(me, target, "a stop policy imports no clock or I/O"))
            elif _within(target, "aksha.runtime.storage") and not target.endswith(".rows"):
                out.append(Violation(me, target, "only commit.py reaches the event store"))
            elif _within(target, "aksha.adapters"):
                out.append(Violation(me, target, "a stop policy imports no adapter"))
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"), filename=str(f))):
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                if called in PURE_STOP_CALLS:
                    out.append(Violation(me, str(called), "a stop policy calls no clock or I/O"))
    return out


def bypass_paths(root: Path) -> list[Violation]:
    """Every runtime function that runs a tool body without an ``Allow`` from ``check`` first.

    A call of ``call_body`` (the one way the runtime runs a body, ``tools/bodies.py``) must
    be guarded by the gate's answer, narrowed to ``Allow``: the answer is a name bound from
    ``check(...)`` in the same function, and the call sits in one of three places:

    1. inside ``if isinstance(decision, Allow):`` (its body, not its ``else``);
    2. inside ``case Allow(...):`` of ``match decision:``;
    3. after ``if not isinstance(decision, Allow):`` whose body returns or raises, in the
       same block.

    Calling ``check`` without honouring its answer (``if check(...):``, which is truthy for
    a ``Refuse``) is a bypass. ``bodies.py`` itself is exempt.
    """
    out: list[Violation] = []
    for f in sorted((root / "aksha" / "runtime").rglob("*.py")):
        if f.relative_to(root).as_posix() == "aksha/runtime/tools/bodies.py":
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            answers = _check_answers(fn)
            for call in _calls_named(fn, "call_body"):
                if not _guarded(fn, call, answers):
                    out.append(
                        Violation(module_name(root, f), fn.name, "every body runs after an Allow")
                    )
                    break
    return out


def _calls_named(fn: ast.AST, name: str) -> list[ast.Call]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _called(n) == name]


def _check_answers(fn: ast.AST) -> set[str]:
    """Names bound to the result of ``check(...)`` in ``fn`` (``d = check(...)``, walrus)."""
    names: set[str] = set()
    for node in ast.walk(fn):
        value = getattr(node, "value", None)
        if not (isinstance(value, ast.Call) and _called(value) == "check"):
            continue
        if isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign | ast.NamedExpr) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _is_allow_test(test: ast.expr, answers: set[str]) -> bool:
    """``isinstance(<answer>, Allow)``."""
    return (
        isinstance(test, ast.Call)
        and _called(test) == "isinstance"
        and len(test.args) == 2
        and isinstance(test.args[0], ast.Name)
        and test.args[0].id in answers
        and _last_name(test.args[1]) == "Allow"
    )


def _last_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _exits(body: list[ast.stmt]) -> bool:
    return bool(body) and isinstance(body[-1], ast.Return | ast.Raise)


def _guarded(fn: ast.AST, call: ast.Call, answers: set[str]) -> bool:
    """Whether ``call`` is reached only when the gate's answer is an ``Allow``."""
    parents = {child: node for node in ast.walk(fn) for child in ast.iter_child_nodes(node)}
    node: ast.AST = call
    while node in parents:
        parent = parents[node]
        if (
            isinstance(parent, ast.If)
            and node in parent.body
            and _is_allow_test(parent.test, answers)
        ):
            return True
        if isinstance(parent, ast.match_case) and node in parent.body:
            pattern, match = parent.pattern, parents.get(parent)
            subject = match.subject if isinstance(match, ast.Match) else None
            if (
                isinstance(pattern, ast.MatchClass)
                and _last_name(pattern.cls) == "Allow"
                and isinstance(subject, ast.Name)
                and subject.id in answers
            ):
                return True
        block = next(
            (
                b
                for field in ("body", "orelse", "finalbody")
                if node in (b := getattr(parent, field, [])) and isinstance(b, list)
            ),
            None,
        )
        if block is not None:
            for earlier in block[: block.index(node)]:
                test = earlier.test if isinstance(earlier, ast.If) else None
                if (
                    isinstance(test, ast.UnaryOp)
                    and isinstance(test.op, ast.Not)
                    and _is_allow_test(test.operand, answers)
                    and _exits(earlier.body)
                ):
                    return True
        node = parent
    return False


def _called(node: ast.Call) -> str | None:
    fn = node.func
    return fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)


def model_text_in(cls: type, forbidden: frozenset[type], namespace: dict[str, object]) -> list[str]:
    """Dotted field paths under the dataclass ``cls`` whose type is one of ``forbidden``.

    Walks every field's type hint, through ``Mapping``, ``tuple``, unions and nested
    dataclasses. A ``Callable``'s signature is not walked: a declared function is not data
    the gate holds. ``namespace`` resolves names imported only under ``TYPE_CHECKING``.
    """
    import collections.abc
    import dataclasses
    import typing

    found: list[str] = []
    seen: set[type] = set()

    def walk(hint: object, path: str) -> None:
        origin = typing.get_origin(hint)
        if origin is collections.abc.Callable:
            return
        if isinstance(hint, type):
            if hint in forbidden:
                found.append(path)
                return
            if dataclasses.is_dataclass(hint) and hint not in seen:
                seen.add(hint)
                hints = typing.get_type_hints(hint, globalns=namespace)
                for f in dataclasses.fields(hint):
                    walk(hints[f.name], f"{path}.{f.name}")
            return
        for arg in typing.get_args(hint):
            walk(arg, path)

    walk(cls, cls.__name__)
    return found


# --- ADR-P02 D3: the injected clock and IDs ----------------------------------------------

AMBIENT_MODULES = frozenset({"random", "uuid", "secrets"})
"""Random sources the runtime core may not import: IDs come from ``IdSource`` instead."""

AMBIENT_CALLS = frozenset(
    {"datetime.now", "datetime.utcnow", "datetime.today", "date.today", "time.time",
     "time.time_ns", "time.sleep", "time.monotonic", "time.monotonic_ns", "time.perf_counter",
     "time.perf_counter_ns", "time.localtime", "time.gmtime"}
)  # fmt: skip
"""Clock reads the runtime core may not make: time comes from the injected ``Clock``."""

AMBIENT_ALLOWED = {
    ("aksha.runtime.storage.chain", "secrets"): (
        "new_store_id: a store's identity, written once when the store is created, is not a "
        "fact of any run, and must differ between two stores (ADR-P01 H4)"
    ),
}
"""The one exemption, with its reason. A run's log never holds a value from it."""


def ambient_clock_or_randomness(root: Path) -> list[Violation]:
    """Every clock read or random source in ``aksha/runtime`` outside the injected ones.

    Catches ``import random`` and ``from uuid import uuid4``, ``datetime.now()`` and
    ``time.sleep()`` called through their modules, and ``from time import sleep``.
    """
    out: list[Violation] = []
    for f in sorted((root / "aksha" / "runtime").rglob("*.py")):
        me = module_name(root, f)
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            found: list[str] = []
            if isinstance(node, ast.Import):
                found = [a.name for a in node.names if a.name.split(".")[0] in AMBIENT_MODULES]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                top = node.module.split(".")[0]
                names = [f"{node.module}.{a.name}" for a in node.names]
                found = (
                    [top] if top in AMBIENT_MODULES else [n for n in names if n in AMBIENT_CALLS]
                )
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                held = node.func.value
                owner = held.attr if isinstance(held, ast.Attribute) else getattr(held, "id", "")
                if f"{owner}.{node.func.attr}" in AMBIENT_CALLS:
                    found = [f"{owner}.{node.func.attr}"]
            for target_name in found:
                if (me, target_name) not in AMBIENT_ALLOWED:
                    rule = "the runtime reads time and randomness only through Clock and IdSource"
                    out.append(Violation(me, target_name, rule))
    return out


# --- ADR-P14 D1: no counter field for any cap ---------------------------------------------


def _held_target(node: ast.expr) -> str | None:
    """``self.n`` or ``self.counts[k]``: state an object keeps between calls."""
    while isinstance(node, ast.Subscript):
        node = node.value
    root = node
    while isinstance(root, ast.Attribute):
        root = root.value
    if isinstance(node, ast.Attribute) and isinstance(root, ast.Name) and root.id == "self":
        return ast.unparse(node)
    return None


def counter_fields(root: Path) -> list[Violation]:
    """Every attribute of a runtime object that is counted up or down in place.

    A cap kept as ``self.used += 1`` resets when the process does (ADR-P14 D1): every count
    is a reducer over the event store instead. Catches ``self.n += 1``,
    ``self.counts[k] -= 1`` and ``self.n = self.n + 1``; a local variable is not state.
    """
    out: list[Violation] = []
    for f in sorted((root / "aksha" / "runtime").rglob("*.py")):
        me = module_name(root, f)
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"), filename=str(f))):
            held: str | None = None
            if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add | ast.Sub):
                held = _held_target(node.target)
            elif isinstance(node, ast.Assign) and isinstance(node.value, ast.BinOp):
                for target in node.targets:
                    name = _held_target(target)
                    operands = {ast.unparse(node.value.left), ast.unparse(node.value.right)}
                    if name is not None and name in operands:
                        held = name
            if held is not None:
                out.append(Violation(me, held, "a cap is counted from the event store (P14 D1)"))
    return out
