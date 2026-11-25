"""Tool values: the tool spec, its declaration, what a body sees and returns (ADR-P10).

A ``ToolSpec`` is everything the runtime knows about a tool before calling it; a
``ToolDecl`` pairs it with the body, a plain function the runtime only calls. A
``ToolUse`` names a tool declared elsewhere (loader-added, or contributed by an extension as
``ext.name``). An agent lists either.

A body is called with a ``ToolContext`` and the validated arguments and returns a
``ToolOutcome``: an ``Ok`` or an ``Err`` result, plus any ``SaveRequest``s. The context
has no store, model or adapter handle, so a body cannot write the event store.

``ToolCall`` is one call the model asked for, the policy gate's only model-written input
(ADR-P11 D4). ``ToolView`` is what the model is shown for one tool; ``ToolResultView`` is the capped
model view of one result. ``ToolContext`` gains its readers, deadline and clock with the
durable step executor (ADR-P02), and ``Ok`` its excerpt drafts with grounding (ADR-P13).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from aksha.runtime.artifacts.types import SaveRequest
from aksha.runtime.interrupts.types import ApprovalRule
from aksha.runtime.outbox.types import EffectSpec
from aksha.runtime.sandbox.types import SandboxNeeds

EffectClass = Literal["read", "store", "external", "exec", "model", "reserved"]
"""The one category of effect a tool has; it picks the gate's route and the recovery."""

EFFECT_CLASSES: frozenset[str] = frozenset(get_args(EffectClass))
"""The six names, for checks of values that arrive untyped from an agent module."""


@dataclass(frozen=True)
class RetryPolicy:
    """Timeout and retries for one tool, counted from the event store (ADR-P14)."""

    timeout_ms: int = 10_000
    max_retries: int = 2
    retry_on: frozenset[str] = frozenset({"timeout", "transient"})
    backoff_ms: tuple[int, ...] = (500, 1_000)


@dataclass(frozen=True)
class ToolSpec:
    """Everything the runtime knows about a tool before calling it. ``schema`` is JSON Schema.

    ``schema`` (Draft 2020-12, an object schema) is shown to the model as is and is the
    schema arguments are validated against. ``external`` is required exactly when
    ``effect`` is ``external``; ``approval`` requires ``external``; ``sandbox`` is for
    ``exec`` tools and ``model_alias`` for ``model`` tools only. ``result_key`` is a JSON
    pointer to a hit's stable identity in a recorded query; ``egress`` names the egress
    policy of a tool whose body reaches the network.
    """

    name: str
    owner: str
    effect: EffectClass
    description: str
    schema: Mapping[str, Any]
    collections: frozenset[str] = frozenset()
    reads_kinds: frozenset[str] = frozenset()
    writes_kinds: frozenset[str] = frozenset()
    mints_citation_spans: bool = False
    recorded_query: bool = False
    retry: RetryPolicy = RetryPolicy()
    result_key: str | None = None
    external: EffectSpec | None = None
    approval: ApprovalRule | None = None
    egress: str | None = None
    sandbox: SandboxNeeds | None = None
    model_alias: str | None = None


@dataclass(frozen=True)
class ToolContext:
    """Who and where a call runs for. ``owner`` comes from the session, never from arguments."""

    owner: str
    agent: str
    task_path: str
    key: str | None
    step_id: str
    attempt: int


@dataclass(frozen=True)
class Ok:
    """A successful result. ``data`` is what the model reads, as data, never as instructions."""

    data: Mapping[str, Any]


@dataclass(frozen=True)
class Err:
    """A failed result the model reads in its next turn and can correct against.

    ``code`` is stable (``schema``, ``tool_exception``, ``class_violation``, or the tool's
    own); ``paths`` lists every failing argument path; ``item`` is the item left unverified.
    """

    code: str
    message: str
    paths: tuple[str, ...] = ()
    item: str | None = None


@dataclass(frozen=True)
class ToolOutcome:
    """What a body returns: one result and the saves it asks for (store and exec tools only)."""

    result: Ok | Err
    saves: tuple[SaveRequest, ...] = ()


ToolBody = Callable[[ToolContext, Mapping[str, Any]], ToolOutcome]
"""``(context, validated arguments) -> outcome``. The arguments are read-only."""


@dataclass(frozen=True)
class ToolDecl:
    """A declared tool: its spec and body. ``body`` is None only for model and reserved tools."""

    spec: ToolSpec
    body: ToolBody | None


@dataclass(frozen=True)
class ToolUse:
    """A tool an agent lists by name, with optional per-agent retry and timeout overrides.

    ``retries`` and ``timeout_ms`` replace ``max_retries`` and ``timeout_ms`` of the
    contributed tool's ``RetryPolicy`` in this agent's registry (so they change the
    definition hash). The loader refuses them on a loader-added tool.
    """

    tool: str
    retries: int | None = None
    timeout_ms: int | None = None


@dataclass(frozen=True)
class ToolCall:
    """One call the model asked for: a tool name, its arguments and its position in the turn."""

    name: str
    arguments: Mapping[str, Any]
    position: int = 0


@dataclass(frozen=True)
class ToolView:
    """One tool as the model is shown it: the name it calls, a description and the schema."""

    name: str
    description: str
    schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolResultView:
    """The model view of one result, capped when recorded (D7).

    ``text`` is the canonical JSON of the result, cut to the cap with head and tail kept;
    ``full_chars`` and ``full_hash`` describe the uncut text and ride on ``step.completed``.
    """

    status: Literal["ok", "error"]
    text: str
    truncated: bool
    full_chars: int
    full_hash: str
