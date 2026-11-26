"""Reading a document nobody described in advance: decide its kind, pick its schema, extract.

ADR-P07 amendment G3. A person uploads whatever they have, so no single extraction schema
fits every record. A read tool that must understand a record runs three fast-class steps
through ``ToolContext.models`` (P07 G1), each recorded on the tool step:

1. **Decide.** A classification alias (the agent's *decide* alias) answers one choice
   question: which of the agent's declared :class:`DocumentKind` values the record is,
   or :data:`OTHER_KIND`. The state is a preview code renders from structure alone
   (:func:`document_preview`: the record's name and format, its headings, a table's columns with
   sample values, the start of its text), never a reading of what it means. The answer
   is judged by the question's thresholds like any classification: under ``accept_at``,
   inside the abstention band, it abstains, and the record is read as :data:`OTHER_KIND`.
2. **Pick the schema.** Each kind names the extraction alias whose declared fields are
   that kind's typed schema (``text``, ``number``, ``date``, ``time``, ``datetime``,
   ``duration``, ``list``, ``estimate``, ``forecast``). Which model answers an alias is
   configuration (the binding), never agent code. A kind with no extraction alias, and
   :data:`OTHER_KIND`, has no schema.
3. **Extract.** The kind's alias fills its schema from the record's passages. Every value
   comes back with spans inside the passages it was shown, which :func:`span_draft` turns
   into ranges of the record, so a value is cited to the words that state it and the
   grounding layer verifies each quote verbatim at save (P13 D2).

What the fast steps leave is **for the System 2 model** (the Task's general model): a
record whose kind is :data:`OTHER_KIND` or undecided, and every schema field the extractor
abstained on, are listed in :attr:`DocumentReading.unresolved`. The tool returns the
record's passages beside the reading, one citation span each, so the general model reads
what the fast models could not and cites it the same way. Nothing a fast model abstained
on is guessed in code.

The same pipeline runs whichever adapter answers. With a native System 1 model bound to
the decide and extract aliases it is fast and calibrated; with only a general model, the
adapted adapters fill the same contracts under a forced schema (P07 D5 as amended G3),
their quotes located in code, judged by the adapted thresholds. Offline, each agent's
fakes implement the same contracts with rules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aksha.runtime.grounding.segment import Segment, read_rows, visible
from aksha.runtime.grounding.types import ReturnedText, SpanDraft
from aksha.runtime.models.calls import span_draft
from aksha.runtime.models.types import ConfidenceThresholds, Judgement, QuestionSpec

if TYPE_CHECKING:
    from aksha.runtime.tools.ports import ToolModels

OTHER_KIND = "other"
"""The kind of a record no declared kind fits, or whose kind the decision abstained on."""

KIND_QUESTION = "kind"
"""The decide alias's one question: which kind the record is."""

PREVIEW_CHARS = 2_400
_TYPE = re.compile(r'"@type"\s*:\s*"([^"]{1,40})"')
"""How much of a record's text the decide step's preview shows."""


@dataclass(frozen=True)
class DocumentKind:
    """One kind of record an agent reads, and the extraction alias holding its schema.

    ``description`` is shown to the decide model as the kind's meaning. ``extract`` names
    an extraction alias the agent declares (its fields are the schema); None means the
    kind is recognised but read by the System 2 model alone.
    """

    name: str
    description: str
    extract: str | None = None


def kind_question(
    kinds: Sequence[DocumentKind], thresholds: ConfidenceThresholds | None = None
) -> QuestionSpec:
    """The decide question over ``kinds`` and :data:`OTHER_KIND`, each kind's meaning in the prompt."""  # noqa: E501
    lines = ["Which kind of document is this? The kinds:"]
    lines += [f"- {k.name}: {k.description}" for k in kinds]
    lines.append(f"- {OTHER_KIND}: none of the above")
    options = tuple(dict.fromkeys([*(k.name for k in kinds), OTHER_KIND]))
    q = QuestionSpec(KIND_QUESTION, "\n".join(lines), "choice", options)
    return (
        q
        if thresholds is None
        else QuestionSpec(q.name, q.prompt, q.type, q.options, thresholds=thresholds)
    )


def document_preview(record_id: str, fmt: str, text: str, segments: Sequence[Segment]) -> str:
    """The decide step's state: the record as structure shows it, never what it means.

    The name and format; for a table its top-level scalar fields (a JSON object's), its
    columns and a few values of each; for a text its headings, its header fields (front
    matter, email headers, meta tags) with short values, the types its structured data
    declares, and the first :data:`PREVIEW_CHARS` characters a reader would see.
    """
    lines = [f"record: {record_id}", f"format: {fmt}"]
    if fmt in ("csv", "json"):
        try:
            table = read_rows(text, fmt)
        except ValueError:
            table = None
        if table is not None and table.header:
            shown = [f"{k}={' '.join(str(v).split())[:60]}" for k, v in table.header.items()]
            lines.append("header fields: " + "; ".join(shown[:24]))
        if table is not None and table.columns:
            lines.append(f"rows: {len(table.rows)}")
            lines.append("columns and sample values:")
            for c in table.columns:
                sample = [r.cells[c][0] for r in table.rows[:5] if c in r.cells and r.cells[c][0]]
                lines.append(f"- {c}: " + " | ".join(v[:100] for v in sample))
            return "\n".join(lines)
    heads = [s.text for s in segments if s.kind in ("title", "heading")][:20]
    if heads:
        lines.append("headings: " + " / ".join(h[:80] for h in heads))
    keys: dict[str, str] = {}
    for s in segments:
        if s.kind != "meta":
            continue
        key = str(s.attrs.get("key") or s.attrs.get("property") or s.attrs.get("name") or "")
        value = str(s.attrs.get("value") or s.attrs.get("content") or "")
        if key and key not in keys:
            keys[key] = " ".join(value.split())[:60]
    if keys:
        shown = [f"{k}={v}" if v else k for k, v in list(keys.items())[:24]]
        lines.append("header fields: " + "; ".join(shown))
    types = sorted({t for s in segments if s.kind == "data" for t in _TYPE.findall(s.text)})
    if types:
        lines.append("structured data types: " + ", ".join(types[:12]))
    rows = [s for s in segments if s.kind in ("row", "header")]
    if rows:
        lines.append(f"table rows: {len(rows)}")
    body: list[str] = []
    used = 0
    for s in segments:
        if s.kind in ("meta", "data", "hidden"):
            continue
        piece = visible(s.text) if fmt == "html" else " ".join(s.text.split())
        if not piece:
            continue
        body.append(piece)
        used += len(piece) + 1
        if used >= PREVIEW_CHARS:
            break
    lines.append("text: " + " ".join(body)[:PREVIEW_CHARS])
    return "\n".join(lines)


MAX_SECTIONS = 12
"""The most sections a record is cut into; any further headings stay inside the last one."""

_CONTENT = frozenset({"paragraph", "item", "row", "header", "text", "quote", "code", "hidden"})


def record_sections(segments: Sequence[Segment]) -> list[tuple[int, int]]:
    """The record's sections as ``[start, end)`` ranges of ``segments``, cut by headings alone.

    One record often holds several kinds at once: a memo with a section on who sells what,
    a section of customer quotes and a section of plans; a saved page with a price table
    above its reviews. The decide step answers one kind per state, so a read tool decides
    each section on its own (ADR-P07 G4). A section starts at every heading of the
    shallowest level that occurs at least twice (with none, at every heading); one with no
    content of its own (a title above the first section) joins the one after it. A record
    with fewer than two sections is one section. Nothing here reads what a heading says.
    (Not a record *part* of ADR-P05 D6: a section is a reading unit, versioned with its
    record.)
    """
    heads = [(i, s.level) for i, s in enumerate(segments) if s.kind == "heading"]
    levels = [lv for _, lv in heads]
    split = next((lv for lv in sorted(set(levels)) if levels.count(lv) >= 2), None)
    starts = [i for i, lv in heads if split is None or lv <= split]
    bounds = sorted({0, *starts})
    parts = [(a, b) for a, b in zip(bounds, [*bounds[1:], len(segments)], strict=True) if b > a]
    merged: list[tuple[int, int]] = []
    carry: int | None = None
    for a, b in parts:
        a = a if carry is None else carry
        if not any(segments[i].kind in _CONTENT for i in range(a, b)):
            carry = a
            continue
        carry = None
        merged.append((a, b))
    if carry is not None and merged:
        merged[-1] = (merged[-1][0], len(segments))
    if len(merged) > MAX_SECTIONS:
        merged = [*merged[: MAX_SECTIONS - 1], (merged[MAX_SECTIONS - 1][0], len(segments))]
    return merged if len(merged) >= 2 else [(0, len(segments))]


def section_heading(segments: Sequence[Segment], part: tuple[int, int]) -> str:
    """The text of the section's first heading, or ``""``: a label for a listing, never a test."""
    return next(
        (segments[i].text for i in range(*part) if segments[i].kind in ("heading", "title")), ""
    )


@dataclass(frozen=True)
class DocumentReading:
    """What the fast steps read from one record (module doc).

    ``kind`` is a declared kind's name or :data:`OTHER_KIND`; ``decided`` whether the decision
    was accepted (an abstention reads as :data:`OTHER_KIND`); ``decision`` the decide step's
    reason. ``values`` and ``ranges`` hold the schema's accepted fields and each one's
    record ranges (a list field has one range per item). ``unresolved`` names what is
    left for the System 2 model: ``"kind"`` when the record's kind is :data:`OTHER_KIND`, and
    each schema field the extractor abstained on.
    """

    kind: str
    decided: bool
    decision: str
    values: Mapping[str, Any] = field(default_factory=dict)
    ranges: Mapping[str, tuple[tuple[int, int], ...]] = field(default_factory=dict)
    unresolved: tuple[str, ...] = ()

    @property
    def needs_reader(self) -> bool:
        """Whether the System 2 model must read the record's passages itself."""
        return bool(self.unresolved)

    def first(self, name: str) -> tuple[Any, tuple[int, int] | None]:
        """A single field's value and its first range; (None, None) when unknown."""
        r = self.ranges.get(name, ())
        return self.values.get(name), (r[0] if r else None)

    def as_json(self) -> dict[str, Any]:
        """The reading as a tool result shows it: kind, decision and what is unresolved."""
        return {
            "kind": self.kind,
            "decided": self.decided,
            "decision": self.decision,
            "unresolved": list(self.unresolved),
        }


def decide_kind(
    models: ToolModels | None,
    alias: str,
    kinds: Sequence[DocumentKind],
    state: str,
    thresholds: ConfidenceThresholds | None = None,
) -> tuple[str, bool, str]:
    """``(kind, decided, reason)``: step 1 of the module doc; :data:`OTHER_KIND` on abstention."""
    if models is None:
        return OTHER_KIND, False, "no_model"
    names = {k.name for k in kinds}
    judged = models.classify(alias, state, [kind_question(kinds, thresholds)])
    j = next((x for x in judged if x.name == KIND_QUESTION), None)
    if j is None:
        return OTHER_KIND, False, "missing_answer"
    if j.accepted and isinstance(j.value, str) and (j.value in names or j.value == OTHER_KIND):
        return j.value, True, j.reason
    return OTHER_KIND, False, j.reason


def accepted_ranges(
    judged: Sequence[Judgement], passages: Sequence[SpanDraft]
) -> tuple[dict[str, Any], dict[str, tuple[tuple[int, int], ...]], list[str]]:
    """The accepted values with their record ranges, and the names that abstained."""
    values: dict[str, Any] = {}
    ranges: dict[str, tuple[tuple[int, int], ...]] = {}
    abstained: list[str] = []
    for j in judged:
        if not j.accepted:
            abstained.append(j.name)
            continue
        if j.value is None:
            continue  # accepted "absent": the record does not state it
        got = [d for d in (span_draft(ref, passages) for ref in j.spans) if d is not None]
        values[j.name] = j.value
        ranges[j.name] = tuple((d.start, d.end) for d in got)
    return values, ranges, abstained


def read_document(
    models: ToolModels | None,
    decide: str,
    kinds: Sequence[DocumentKind],
    text: ReturnedText,
    passages: Sequence[SpanDraft],
    state: str,
    *,
    kind: str | None = None,
    thresholds: ConfidenceThresholds | None = None,
) -> DocumentReading:
    """Decide the record's kind (unless ``kind`` is given), pick its schema, extract it.

    ``state`` is the decide step's preview (:func:`document_preview`); ``passages`` are the drafts
    the extractor is shown, in order. Returns what was read and what is left for the
    System 2 model (module doc).
    """
    by_name = {k.name: k for k in kinds}
    if kind is None:
        kind, decided, reason = decide_kind(models, decide, kinds, state, thresholds)
    else:
        decided, reason = kind in by_name or kind == OTHER_KIND, "given"
    chosen = by_name.get(kind)
    if chosen is None or chosen.extract is None or models is None or not passages:
        unresolved = ("kind",) if kind == OTHER_KIND else ()
        return DocumentReading(kind, decided, reason, unresolved=unresolved)
    judged = models.extract(chosen.extract, text, passages)
    values, ranges, abstained = accepted_ranges(judged, passages)
    return DocumentReading(kind, decided, reason, values, ranges, tuple(abstained))
