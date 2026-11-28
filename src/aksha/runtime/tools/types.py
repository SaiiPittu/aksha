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
model view of one result. ``ToolContext.reader`` is the attempt's record reader (ADR-P13
D1); its deadline and clock come with the durable step executor (ADR-P02). ``Ok.spans``
holds the span drafts a read or fetch tool asks the runtime to mint (ADR-P13 D1).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, get_args

from aksha.runtime.artifacts.ports import ArtifactReader
from aksha.runtime.artifacts.types import InputRef, SaveRequest
from aksha.runtime.egress.client import RefusingHttpClient
from aksha.runtime.egress.ports import HttpClient
from aksha.runtime.grounding.ports import RecordReader
from aksha.runtime.grounding.types import SpanDraft
from aksha.runtime.interrupts.types import ApprovalRule
from aksha.runtime.outbox.types import EffectSpec
from aksha.runtime.sandbox.ports import ExecAPI
from aksha.runtime.sandbox.types import SandboxNeeds
from aksha.runtime.timers.reads import TimeReads
from aksha.runtime.web.attempt import AttemptWeb

if TYPE_CHECKING:
    from aksha.runtime.tools.ports import ToolModels

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
class EditSpec:
    """What a tool edits or overwrites: one record of a versioned source (decision 38).

    ``record`` is a JSON pointer to the argument that names the record; with
    ``collection`` the argument is a path inside it and the record ID is
    ``<collection>/<argument>``. ``overwrite`` marks a tool that replaces the whole record
    (a write) rather than changing part of it against text the model quotes (an edit).
    In a run whose agent declared ``edit_guard`` or more, the runtime checks the target
    before the body runs (``invalidation.guard``): a record whose version changed since
    the instance last read it is refused, and so is an overwrite of an existing record the
    instance never read. ``source`` is the versioned source the record is read through.
    """

    source: str
    record: str
    collection: str | None = None
    overwrite: bool = False


@dataclass(frozen=True)
class ToolSpec:
    """Everything the runtime knows about a tool before calling it. ``schema`` is JSON Schema.

    ``schema`` (Draft 2020-12, an object schema) is shown to the model as is and is the
    schema arguments are validated against. ``external`` is required exactly when
    ``effect`` is ``external``; ``approval`` requires ``external``; ``sandbox`` is for
    ``exec`` tools and ``model_alias`` for ``model`` tools only. ``result_key`` is a JSON
    pointer to a hit's stable identity in a recorded query; ``egress`` names the egress
    policy of a tool whose body reaches the network. ``model_aliases`` names the agent's
    extraction and classification aliases the body may ask through ``ToolContext.models``
    (ADR-P07 G1); the loader refuses one the agent does not declare. ``edits`` names the
    record a ``store`` or ``exec`` tool edits or overwrites, for the edit-time check of
    decision 38 (``EditSpec``).
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
    model_aliases: frozenset[str] = frozenset()
    edits: EditSpec | None = None


@dataclass(frozen=True)
class ToolContext:
    """Who and where a call runs for. ``owner`` comes from the session, never from arguments.

    ``reader`` reads versioned records for this attempt (ADR-P13 D1): a body that mints
    citation spans drafts them over the texts it returned. None when no source is
    configured, and then every read returns None.

    ``run_date`` is the ISO date (``YYYY-MM-DD``) the run counts to: the later of its
    recorded ``run.started`` date and the last date the runtime recorded for the owner,
    read from the log as of the step (ADR-P25 B1, amended by B2), so a body that counts
    days counts them to a recorded date, the same on every replay. Reading it is a recorded
    read (ADR-P25 D4, B2): with ``time_reads`` set (the agent loop sets it for every
    attempt), each read adds ``time:clock@<date>`` to the attempt's reads, and a later
    recorded date invalidates what the body's step fed. ``observe_time(ctx)`` is the same
    read, returned as the lineage input.

    ``http`` is the only way a body reaches the network (ADR-P26 D2): bound to the tool's
    egress policy, or refusing everything when the tool names none. ``exec`` runs a program
    in a fresh box under the tool's sandbox policy (ADR-P12); None for a non-exec tool.

    ``artifacts`` reads the owner's versions of the kinds the tool declares in
    ``reads_kinds``, as the log stood before the step started (ADR-P09 D3); None for a
    tool that declares none. What a body reads through it is lineage only when the body
    returns it in ``Ok.reads``.

    ``models`` asks the fast-class aliases the tool declares (``ToolSpec.model_aliases``),
    recorded on the tool step (ADR-P07 G1); None for a tool that declares none.
    ``web`` searches and fetches the web through the process's web ports (ADR-P26 A3): a
    fetched page is kept as a text the attempt's reader returned, so its passages may be
    drafted as spans; a search hit never is. None when the process has no web port.
    ``environment`` is the environment's own note on the supplied records, when the process
    states one (``ExecutionTier.inputs_environment``, a fixture world's word on how to read
    its records); the input tools return it with every result. It comes from the
    environment, never from an agent's prompt.
    """

    owner: str
    agent: str
    task_path: str
    key: str | None
    step_id: str
    attempt: int
    reader: RecordReader | None = None
    run_date: str | None = None
    http: HttpClient = field(default_factory=RefusingHttpClient)
    exec: ExecAPI | None = None
    time_reads: TimeReads | None = None
    artifacts: ArtifactReader | None = None
    models: ToolModels | None = None
    web: AttemptWeb | None = None
    environment: str = ""

    def __getattribute__(self, name: str) -> Any:
        """Every read of ``run_date`` is recorded in ``time_reads`` (class doc)."""
        value = object.__getattribute__(self, name)
        if name == "run_date" and value is not None:
            reads = object.__getattribute__(self, "time_reads")
            if reads is not None:
                reads.observe(value)
        return value


@dataclass(frozen=True)
class Ok:
    """A successful result. ``data`` is what the model reads, as data, never as instructions.

    ``spans`` are span drafts over texts the runtime's reader returned; only a tool that
    declares ``mints_citation_spans`` may return them, and the runtime mints them (ADR-P13).
    ``reads`` are the versioned inputs the body read (a source record at its version, a
    recorded query and its result-set hash, an artifact version): the agent loop records
    them as the instance's reads in the step's result (ADR-P04 D6), so lineage reaches
    the source and a source change invalidates what was built from it (ADR-P05).
    """

    data: Mapping[str, Any]
    spans: tuple[SpanDraft, ...] = ()
    reads: tuple[InputRef, ...] = ()


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
