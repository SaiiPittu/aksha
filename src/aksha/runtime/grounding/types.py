"""Grounding values (ADR-P13): span drafts, citation spans, the cite forms, explicit unknowns.

A tool body returns ``SpanDraft`` values beside its result; the runtime checks each against
a ``ReturnedText`` its reader returned and mints a ``CitationSpan``. Saved content cites
with one of three forms, written inside an object's ``cite`` field:

1. ``{"cite": {"span": "EX-0042", "quote": "279 EUR per sensor"}}`` (``SpanCite``);
2. ``{"cite": {"statement": "EX-0007"}}``, a person's answer minted as a span of its own
   (``StatementCite``);
3. ``{"cite": {"artifact": "notes.matrix@v1", "path": "/rows/3"}}`` (``ArtifactCite``).

A field the agent could not ground holds ``{"unknown": {...}}`` (``Unknown``) where its
``CitePolicy`` says ``may_be_unknown``. Every value here is frozen; nothing holds a store.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, get_args

MIN_QUOTE_CHARS = 12
"""The shortest quote a citation may use, unless it quotes the whole span (D2)."""

SPAN_PREFIX = "EX"
"""Span IDs are ``EX-<n:04d>``, counted per owner from the event store."""

SpanSource = str
"""Where a span's text came from: ``fixtures``, ``artifact``, ``tree``, ``exec``,
``statement`` or a source an agent declares. The core treats them all alike (D3)."""

STATEMENT_SOURCE = "statement"
"""The source of a person's answer minted as a span (a user statement)."""

UnknownReason = Literal["not_found", "unverified", "not_checked", "not_located"]
"""Why a field holds no value (D5). ``not_located`` is the fast model's demotion (D6)."""

UNKNOWN_REASONS: frozenset[str] = frozenset(get_args(UnknownReason))

CiteFormName = Literal["span", "statement", "artifact"]
"""The three cite forms (D4)."""

CITE_FORMS: frozenset[str] = frozenset(get_args(CiteFormName))

CiteCode = Literal[
    "unknown_span",
    "span_not_seen",
    "stale_source",
    "quote_not_verbatim",
    "quote_too_short",
    "form_not_allowed",
    "superseded_statement",
    "stale_artifact_cite",
    "ungrounded_value",
    "cite_invalid",
    "span_not_of_source",
    "value_not_in_quote",
    "unknown_not_allowed",
    "unknown_invalid",
]
"""Every refusal the verifier gives. The ADR's nine, plus ``cite_invalid`` (a cite object
of no form), ``span_not_of_source`` (a relation excerpt whose span is another record's),
``value_not_in_quote`` (S3), ``unknown_not_allowed`` and ``unknown_invalid`` (D5)."""

_POINTER_PART = re.compile(r"^(\*|[^/]*)$")


@dataclass(frozen=True)
class SpanDraft:
    """A passage a tool body asks the runtime to mint: offsets into one returned text.

    ``start`` and ``end`` count code points into the canonical text the reader returned
    for ``(source, record_id, version)``; ``start < end``.
    """

    source: SpanSource
    record_id: str
    version: str
    start: int
    end: int


@dataclass(frozen=True)
class ReturnedText:
    """One text the runtime's reader returned to a tool body in this attempt.

    The runtime, not the body, builds these: a draft is minted only over a text that is
    here, so a body cannot mint a passage nobody fetched (D1).
    """

    source: SpanSource
    record_id: str
    version: str
    text: str

    def locator(self) -> tuple[str, str, str]:
        """``(source, record_id, version)``: what a draft names."""
        return (self.source, self.record_id, self.version)


@dataclass(frozen=True)
class CitationSpan:
    """A minted span (v2 "excerpt"): the quote selector's exact text and its positions.

    ``tenant`` is the owner. ``shown_to`` holds the ``(run, instance)`` pairs whose shown
    text contained the span (S1): the tool's capped view or a later rendering.
    ``supersedes`` is the statement span a corrected statement replaces.
    """

    span_id: str
    tenant: str
    source: SpanSource
    record_id: str
    version: str
    start: int
    end: int
    text: str
    tool: str
    shown_to: frozenset[tuple[str, str]] = frozenset()
    supersedes: str | None = None

    def locator(self) -> tuple[str, str, str, int, int]:
        """What makes two mints the same span: source, record, version and offsets."""
        return (self.source, self.record_id, self.version, self.start, self.end)

    def as_json(self) -> dict[str, Any]:
        """The span as ``excerpts.minted`` records it (``shown_to`` is a separate list)."""
        out: dict[str, Any] = {
            "span_id": self.span_id,
            "source": self.source,
            "record_id": self.record_id,
            "version": self.version,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "tool": self.tool,
        }
        if self.supersedes is not None:
            out["supersedes"] = self.supersedes
        return out


@dataclass(frozen=True)
class SpanIndex:
    """The owner's spans by ID, read from the event store by ``spans.span_index``."""

    tenant: str
    spans: Mapping[str, CitationSpan] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze the mapping."""
        object.__setattr__(self, "spans", MappingProxyType(dict(self.spans)))

    def get(self, span_id: str) -> CitationSpan | None:
        """The span named ``span_id`` for this tenant, if minted."""
        return self.spans.get(span_id)

    def find(self, locator: tuple[str, str, str, int, int]) -> CitationSpan | None:
        """The span already minted at ``locator``, if any (a repeated read reuses its ID)."""
        for span in self.spans.values():
            if span.locator() == locator:
                return span
        return None

    def superseded(self) -> frozenset[str]:
        """The statement spans a later statement replaces."""
        return frozenset(s.supersedes for s in self.spans.values() if s.supersedes)

    def highest(self) -> int:
        """The highest ``n`` of the ``EX-<n>`` IDs minted so far (0 when none)."""
        return max((int(i.split("-", 1)[1]) for i in self.spans), default=0)


@dataclass(frozen=True)
class SpanCite:
    """Cite a span: ``quote`` must occur in its text verbatim, whitespace runs collapsed."""

    span: str
    quote: str


@dataclass(frozen=True)
class StatementCite:
    """Cite a person's statement: a span minted from an interrupt answer."""

    statement: str


@dataclass(frozen=True)
class ArtifactCite:
    """Cite an artifact version (``kind/key@vN``) at a JSON pointer in its content."""

    artifact: str
    path: str = ""


CiteForm = SpanCite | StatementCite | ArtifactCite
"""One of the three cite forms (D4)."""


def form_name(cite: CiteForm) -> CiteFormName:
    """``span``, ``statement`` or ``artifact``."""
    if isinstance(cite, SpanCite):
        return "span"
    if isinstance(cite, StatementCite):
        return "statement"
    return "artifact"


def parse_cite(value: Any) -> CiteForm | None:
    """The cite form a ``cite`` field holds, or None when it is no well-formed form."""
    if not isinstance(value, Mapping):
        return None
    keys = set(value)
    if keys == {"span", "quote"} and _strs(value, "span", "quote"):
        return SpanCite(value["span"], value["quote"])
    if keys == {"statement"} and _strs(value, "statement"):
        return StatementCite(value["statement"])
    if keys in ({"artifact"}, {"artifact", "path"}) and _strs(value, *keys):
        return ArtifactCite(value["artifact"], value.get("path", ""))
    return None


def _strs(value: Mapping[str, Any], *keys: str) -> bool:
    """Whether each key holds a string, non-empty except ``path`` (``""`` is the whole)."""
    return all(isinstance(value[k], str) and (value[k] or k == "path") for k in keys)


@dataclass(frozen=True)
class Unknown:
    """An explicit unknown (v2 "named gap"): no value, a question, and why (D5).

    ``checked`` lists the reads (``InputRef`` text, ``record:pages/P-0001@1``) read in full
    for ``not_found``; ``failed_step`` names the failed step for ``unverified``.
    """

    question: str
    reason: UnknownReason
    checked: tuple[str, ...] = ()
    failed_step: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The value as content holds it: ``{"unknown": {...}}``."""
        inner: dict[str, Any] = {"question": self.question, "reason": self.reason}
        if self.checked:
            inner["checked"] = list(self.checked)
        if self.failed_step is not None:
            inner["failed_step"] = self.failed_step
        return {"unknown": inner}

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Unknown:
        """Read :meth:`as_json`'s form back (``ValueError`` for anything else)."""
        if set(value) != {"unknown"} or not isinstance(value["unknown"], Mapping):
            raise ValueError("an unknown is {'unknown': {question, reason, ...}}")
        inner = value["unknown"]
        extra = set(inner) - {"question", "reason", "checked", "failed_step"}
        if extra:
            raise ValueError(f"an unknown has no field {sorted(extra)[0]!r}")
        question, reason = inner.get("question"), inner.get("reason")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("an unknown asks a question")
        if reason not in UNKNOWN_REASONS:
            raise ValueError(f"an unknown's reason is one of {sorted(UNKNOWN_REASONS)}")
        checked = inner.get("checked", [])
        if not isinstance(checked, list | tuple) or not all(isinstance(c, str) for c in checked):
            raise ValueError("checked lists reads as text")
        failed = inner.get("failed_step")
        if failed is not None and (not isinstance(failed, str) or not failed):
            raise ValueError("failed_step names a step")
        return cls(question, reason, tuple(checked), failed)


def is_unknown_node(value: Any) -> bool:
    """Whether ``value`` is shaped like an explicit unknown: an object whose one key is it."""
    return isinstance(value, Mapping) and set(value) == {"unknown"}


UNKNOWN_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "unknown": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "minLength": 1},
                    "reason": {"enum": sorted(UNKNOWN_REASONS)},
                    "checked": {"type": "array", "items": {"type": "string"}},
                    "failed_step": {"type": "string", "minLength": 1},
                },
                "required": ["question", "reason"],
                "additionalProperties": False,
            }
        },
        "required": ["unknown"],
        "additionalProperties": False,
    }
)
"""The JSON Schema of an explicit unknown, for an agent's schema of a may-be-unknown field."""


def maybe_unknown(schema: Mapping[str, Any]) -> dict[str, Any]:
    """``schema`` or an explicit unknown: what a ``may_be_unknown`` field's schema says."""
    return {"anyOf": [dict(schema), dict(UNKNOWN_SCHEMA)]}


@dataclass(frozen=True)
class CitePolicy:
    """How one field of an artifact kind is grounded (D4, D5, S3); declared data.

    ``field`` is a JSON Pointer pattern into the content where ``*`` matches any one key
    or index (``/cells/*/price``). Each node it matches must hold a ``cite`` of one of
    ``forms`` or, when ``may_be_unknown``, an explicit unknown; anything else is
    ``ungrounded_value``. ``span_sources`` and ``artifact_kinds``, when set, narrow which
    spans and which artifact kinds the field may cite. ``value_in_quote`` asks that the
    node's ``value`` appear in the span quote it cites, exactly as the source states it.
    """

    field: str
    forms: frozenset[CiteFormName] = frozenset({"span"})
    span_sources: frozenset[str] = frozenset()
    artifact_kinds: frozenset[str] = frozenset()
    may_be_unknown: bool = False
    value_in_quote: bool = False

    def __post_init__(self) -> None:
        """Refuse a pattern that is no JSON pointer and a form list that is empty or wrong."""
        if not self.field.startswith("/"):
            raise ValueError(f"a cite policy's field is a JSON pointer pattern: {self.field!r}")
        if not all(_POINTER_PART.match(p) for p in self.field[1:].split("/")):
            raise ValueError(f"not a pointer pattern: {self.field!r}")
        if not self.forms or not set(self.forms) <= CITE_FORMS:
            raise ValueError(f"forms are a non-empty subset of {sorted(CITE_FORMS)}")
        if self.value_in_quote and "span" not in self.forms:
            raise ValueError("value_in_quote needs the span form")

    def parts(self) -> list[str]:
        """The pattern's parts, unescaped as RFC 6901 says (``*`` stays a wildcard)."""
        return [p.replace("~1", "/").replace("~0", "~") for p in self.field[1:].split("/")]


ANY_FORM = CitePolicy(field="/*", forms=frozenset({"span", "statement", "artifact"}))
"""The policy a cite outside every declared field is checked under: any form, any source."""


@dataclass(frozen=True)
class Verified:
    """A citation that passed: where it is, its form, what it names and the quote's place.

    ``target`` is the span ID, the statement's span ID or the artifact ref; ``occurrence``
    is the 0-based index of the quote's first match in the normalised span text.
    """

    pointer: str
    form: CiteFormName
    target: str
    quote: str | None = None
    occurrence: int | None = None
    path: str | None = None


@dataclass(frozen=True)
class CiteError:
    """A citation or field that failed: the code, the pointer into the content and why."""

    code: CiteCode
    pointer: str
    message: str
