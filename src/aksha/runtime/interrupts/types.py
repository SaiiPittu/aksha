"""Interrupt values: kinds, requests, views, the action body and approval views (ADR-P17).

``DeclaredInterrupt`` and ``ApprovalRule`` are what an agent declares (on a Task and on a
tool). The rest is the runtime's: the six interrupt kinds (D2, S5), what raising one
asks for (``InterruptRequest``), how a person sees it (``InterruptView``), the generic
``ActionBody`` an approval signs (D6) and the approval's own view (``ApprovalView``).

An interrupt's scope is read from its event's envelope, never stored twice: an instance
(``run`` and ``instance`` set), a run (``run`` set, no instance: the budget interrupt) or a
session (``session`` set, no run: the request router's ``route`` question, S5).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

InterruptKind = Literal["input", "choice", "approval", "budget", "review", "route", "correction"]
"""The closed set of interrupt kinds (D2, P17-3); an agent's word goes in a ``label``."""

INTERRUPT_KINDS: tuple[InterruptKind, ...] = (
    "input",
    "choice",
    "approval",
    "budget",
    "review",
    "route",
    "correction",
)

FREE_TEXT_KINDS: frozenset[str] = frozenset({"input", "choice", "route"})
"""The kinds a free-text answer may resolve (D4, IE-10); the rest need an explicit command."""

VoidReason = Literal[
    "key_withdrawn",
    "run_ended",
    "run_stopped",
    "replaced_by_edit",
    "superseded",
    "definition_changed",
    "revision_invalidated",
]
"""Why the runtime withdrew an interrupt; it never resolves one (D4)."""

InterruptState = Literal["open", "resolved", "voided"]
ScopeName = Literal["instance", "run", "session"]


ChoiceOptions = Callable[[Any], Sequence[tuple[str, str]]]
"""Pure: ``TaskView -> ((code, label), ...)``, the options of a declared choice, built by code."""


@dataclass(frozen=True)
class AnswerSave:
    """The artifact the runtime saves from a person's answer to a declared choice.

    ``kind`` is a kind the Task writes; no tool may save it (``answer_only``), and no
    other Task may write it. ``field`` is the top-level field that holds the chosen codes:
    the runtime writes it, as a list of the codes in option order, and the completion
    check ``chosen_by_person`` compares it with the answer. ``build`` is pure,
    ``(TaskView, chosen codes) -> content``; what it returns at ``field`` is replaced.
    """

    kind: str
    field: str
    build: Callable[[Any, tuple[str, ...]], Mapping[str, Any]]


@dataclass(frozen=True)
class DeclaredInterrupt:
    """An interrupt a Task declares; it adds the implicit ``ask`` tool.

    ``label`` is the agent's word for it, rendered to people and never branched on. A
    ``choice`` may declare its ``options`` in code (codes and labels, read from the Task's
    view when the model asks); the model then writes only the question, a summary, and may
    not list options itself. ``multiple`` lets the person choose any subset of the
    options, none included. ``saves`` makes the runtime, not the model, save an artifact
    from the answer (``AnswerSave``): the person's choice is the content.
    """

    kind: Literal["input", "choice"]
    label: str
    options: ChoiceOptions | None = None
    multiple: bool = False
    saves: AnswerSave | None = None

    def __post_init__(self) -> None:
        """Refuse options, ``multiple`` or ``saves`` on anything but a choice."""
        if self.kind != "choice" and (
            self.options is not None or self.multiple or self.saves is not None
        ):
            raise ValueError("only a choice declares options, multiple or saves")
        if self.saves is not None and self.options is None:
            raise ValueError("a choice that saves its answer declares its options in code")


ApprovalFacts = Callable[[Any, Mapping[str, object]], Mapping[str, str]]
"""Pure: ``(TaskView, args) -> facts`` shown to the person and signed (``Any`` until P15)."""


@dataclass(frozen=True)
class ApprovalRule:
    """Declared on a tool (``ToolSpec.approval``): the call needs a person's approval first.

    ``facts`` builds what the person approves; ``targets`` lists the artifact refs the
    action acts on. ``targets`` is None when the action acts on none (the ADR's default
    lambda, written as None so the declared value has a stable form). ``expires_after_s``
    is how long a request (or its grant, until used) stays valid: the request schedules an
    ``approval_expiry`` timer that voids it ``expired`` (ADR-P17 S7, ADR-P25 D5). None,
    the default: it never expires.
    """

    facts: ApprovalFacts
    targets: Callable[[Mapping[str, object]], tuple[str, ...]] | None = None
    expires_after_s: int | None = None

    def __post_init__(self) -> None:
        """Refuse a lifetime below one second."""
        if self.expires_after_s is not None and self.expires_after_s < 1:
            raise ValueError("an approval's expires_after_s is a positive number of seconds")


@dataclass(frozen=True)
class Question:
    """One question of an ``input`` interrupt; S2 gives each its own answer and span."""

    id: str
    text: str

    def as_json(self) -> dict[str, str]:
        """The stored form."""
        return {"id": self.id, "text": self.text}


@dataclass(frozen=True)
class InterruptRequest:
    """What a tool step or the runtime asks to raise: the kind, its scope and what it shows.

    The scope is the envelope's: ``run`` and ``instance`` for an instance, ``run`` alone for
    a run, ``session`` alone for a ``route`` question. ``questions`` (``input``, S2),
    ``options`` (``choice``; the agents a ``route`` lists) and ``label`` are the shown
    parts; ``extra`` holds a kind's own fields (an approval's ID, a review's card).
    """

    kind: InterruptKind
    run: str | None = None
    instance: str | None = None
    session: str | None = None
    questions: tuple[Question, ...] = ()
    options: tuple[str, ...] = ()
    label: str | None = None
    extra: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        """Check the scope: ``route`` alone is session-scoped; ``choice`` lists options."""
        if self.kind == "route":
            if self.session is None or self.run is not None or self.instance is not None:
                raise ValueError("a route interrupt is scoped to a session, with no run")
        elif self.run is None:
            raise ValueError(f"a {self.kind} interrupt is scoped to a run or an instance")
        if self.instance is not None and self.run is None:
            raise ValueError("an instance-scoped interrupt names its run")
        if self.kind in ("choice", "route") and not self.options:
            raise ValueError(f"a {self.kind} interrupt lists its options")
        if self.kind == "input" and not self.questions:
            raise ValueError("an input interrupt asks at least one question")
        ids = [q.id for q in self.questions]
        if len(set(ids)) != len(ids):
            raise ValueError("question IDs are unique within one interrupt")


@dataclass(frozen=True)
class InterruptView:
    """One interrupt as a person and the runtime see it, read from the log (D1)."""

    id: str
    kind: InterruptKind
    run: str | None
    instance: str | None
    session: str | None
    payload: Mapping[str, Any]
    seq: int
    state: InterruptState = "open"
    resolution: Mapping[str, Any] | None = None
    void_reason: str | None = None

    @property
    def scope(self) -> ScopeName:
        """``instance``, ``run`` or ``session``, from the envelope."""
        if self.run is None:
            return "session"
        return "run" if self.instance is None else "instance"

    @property
    def is_open(self) -> bool:
        """True while no closing fact exists."""
        return self.state == "open"


@dataclass(frozen=True)
class ActionBody:
    """The canonical description of the one action an approval covers (D6). Generic.

    Only harness words: the tool, the canonical JSON of its arguments, the target artifact
    refs with their content hashes, the idempotency key of the outbox entry it will create,
    the agent definition hash and the facts the tool's ``ApprovalRule.facts`` returned.
    """

    tool: str
    owner: str
    canonical_args: str
    targets: tuple[tuple[str, str], ...]
    idempotency_key: str
    definition_hash: str
    facts: Mapping[str, str]

    def args(self) -> Mapping[str, Any]:
        """The tool arguments the body names, parsed from their canonical JSON."""
        parsed = json.loads(self.canonical_args)
        if not isinstance(parsed, dict):
            raise ValueError("an action body's arguments are a JSON object")
        return parsed

    def target_refs(self) -> tuple[str, ...]:
        """The artifact refs the action acts on, in order."""
        return tuple(ref for ref, _ in self.targets)

    def as_json(self) -> dict[str, Any]:
        """The stored and signed form: every field, facts sorted by the canonical encoder."""
        return {
            "tool": self.tool,
            "owner": self.owner,
            "canonical_args": self.canonical_args,
            "targets": [[ref, content_hash] for ref, content_hash in self.targets],
            "idempotency_key": self.idempotency_key,
            "definition_hash": self.definition_hash,
            "facts": dict(self.facts),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> ActionBody:
        """Read the stored form back."""
        return cls(
            tool=str(value["tool"]),
            owner=str(value["owner"]),
            canonical_args=str(value["canonical_args"]),
            targets=tuple((str(r), str(h)) for r, h in value["targets"]),
            idempotency_key=str(value["idempotency_key"]),
            definition_hash=str(value["definition_hash"]),
            facts=MappingProxyType({str(k): str(v) for k, v in value["facts"].items()}),
        )


ACTION_BODY_FIELDS: frozenset[str] = frozenset(
    {"tool", "owner", "canonical_args", "targets", "idempotency_key", "definition_hash", "facts"}
)
"""The generic field set; ``test_action_body_has_no_agent_fields`` holds the body to it."""

ApprovalState = Literal["requested", "granted", "rejected", "voided", "revoked", "used"]


@dataclass(frozen=True)
class ApprovalView:
    """One approval and where it is in its life (the ADR's state diagram)."""

    approval_id: str
    body: ActionBody
    signature: str
    state: ApprovalState
    run: str | None
    instance: str | None
    seq: int
    decided_by: str | None = None
    replaces: str | None = None
    revoked_because: tuple[Mapping[str, Any], ...] = ()
    used_by: str | None = None
    void_reason: str | None = None


@dataclass(frozen=True)
class Refusal:
    """Why an approval may not be used or decided: a stable code and a message."""

    code: str
    message: str = ""
