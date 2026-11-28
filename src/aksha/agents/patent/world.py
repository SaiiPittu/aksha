"""The patent agent's document kinds, and the offline model's reading of the inputs.

:data:`KINDS` and :data:`DECIDE` are the agent's declared document kinds and decide alias:
the harness's input and form tools read with them (``tools.READING``, ``tools.FORMS``).
No tool reads through :class:`SourceWorld`: the offline model (``fakes_views``) builds one
over the texts the shared tools returned and works its views out in its own turn, as a
live model reads those texts itself. It never expects a file name, a
folder, a heading wording or a column name. Structure comes from code (``segment``,
``read_rows``: blocks, rows and cells, each an exact range of the raw text); what a record
*holds* comes from the agent's fast-class aliases, asked through ``ToolContext.models``.
First the decide alias ``doc_kind`` names each record's kind (:data:`KINDS`: an invention
disclosure, meeting notes, a contributor or disclosure record, an email, business
priorities, a blank form, a prior-art publication, or ``other``) from a structural
preview (ADR-P07 G3); then only the schema of that kind is extracted:

=====================  ==============  ======================================================
Alias                  Class           What it answers about one record
=====================  ==============  ======================================================
``note_structure``     extraction      whether it describes an invention, and its candidate
                                       ID, title, date, problem, mechanism steps, parameters,
                                       alternatives, evidence and its implementation status,
                                       open points and referenced documents
``table_roles``        classification  what each column of a table holds (:data:`ROLES`)
``candidate_of``       classification  which candidate a row, an item or a person's work is
                                       about, when no cell holds a candidate's ID
``priorities``         extraction      the ranked business priorities, in rank order
``template_fields``    extraction      a blank form's title and fields, which are required and
                                       which only counsel completes (read by the harness's
                                       form tool, ``read_form``)
``publication_biblio`` extraction      a publication's number, kind, office, title, dates,
                                       priority claims, status wordings, claims and text
=====================  ==============  ======================================================

Any format the workspace holds works the same: Markdown, plain text, text extracted from a
PDF, a slide deck or a spreadsheet, an email, a CSV, TSV or semicolon export, JSON. A table
is any delimited record or any pipe table inside a text. A row joins a candidate by the
candidate's ID in a cell, else by ``candidate_of``; a person with no ID column gets one
from their name. Dates are read as people write them (``parse_date``) and kept as written
beside the ISO value. A referenced document is found by its name's words among the input
names, whatever its extension or separators.

A view's quotes are exact substrings of the raw record, which ``read_document`` minted as
units when the model read it, so a completion check verifies every quote an artifact saves
(``checks.ungrounded``).
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from aksha.runtime.api import (
    OTHER_KIND,
    DocumentKind,
    Judgement,
    QuestionSpec,
    RecordReader,
    ReturnedText,
    Segment,
    SpanDraft,
    ToolModels,
    decide_kind,
    document_preview,
    find_dates,
    parse_date,
    read_rows,
    record_sections,
    segment,
    sniff_format,
    span_draft,
)

SOURCE_NAME = "inputs"
COLLECTION = "input"
COLLECTIONS = (COLLECTION,)

ROLES = (
    "record_id", "person_id", "name", "employer", "role", "candidate", "contributed", "date",
    "agreement", "source", "disclosure_id", "event_type", "content_shown", "audience",
    "public", "by", "rank", "other",
)  # fmt: skip
"""What a column of a contributor table, a disclosure history or a priority table holds."""


DECIDE = "doc_kind"
"""The decide alias: which kind of record this is (ADR-P07 G3)."""

KINDS = (
    DocumentKind(
        "invention_disclosure",
        "a description of one invention: what problem it solves and how it works, step by "
        "step (an invention disclosure, a design or engineering note, a slide deck or a "
        "write-up of a technical idea)",
        "note_structure",
    ),
    DocumentKind(
        "meeting_notes",
        "minutes or notes of a meeting, a call or a workshop, possibly describing how an "
        "idea works and who suggested what",
        "note_structure",
    ),
    DocumentKind(
        "contributor_record",
        "a table or list of people and what each contributed to which invention",
    ),
    DocumentKind(
        "disclosure_record",
        "a record of when an invention was shown or published outside the company: a log "
        "of demos, talks, posts, pilots or shipments, or one such event's own write-up",
    ),
    DocumentKind("email", "an email or a message between people"),
    DocumentKind(
        "business_priorities",
        "the business's ranked priorities, goals or roadmap",
        "priorities",
    ),
    DocumentKind(
        "form_template",
        "a blank form or template to fill in (an invention disclosure form, a filing "
        "worksheet, an intake questionnaire)",
        "template_fields",
    ),
    DocumentKind(
        "prior_art_publication",
        "a published patent or patent application, or another publication describing a technique",
        "publication_biblio",
    ),
)
"""The kinds of record the patent agent reads; extend it with a kind and its alias."""

KIND_NAMES = (*(k.name for k in KINDS), OTHER_KIND)
NOTE_KINDS = frozenset({"invention_disclosure", "meeting_notes"})
READ_AS_NOTE = NOTE_KINDS | {OTHER_KIND}
"""Kinds whose records are read for an invention: a record no kind fits may describe one
(plain notes with no headings); the ``note_structure`` schema's own steps say whether it does."""
TABLE_KINDS = frozenset({"contributor_record", "disclosure_record", "meeting_notes", OTHER_KIND})
"""Kinds whose tables (delimited, JSON rows or pipe tables) are read for people and events."""


class NotInWorld(LookupError):
    """A record the tool was asked for is not among the inputs."""


def day_first(text: str) -> str | None:
    """A date as written (any common form, day first when ambiguous) as ISO; else None."""
    found = find_dates(text, "dmy")
    return found[0][0] if found else parse_date(text, "dmy")


def days_between(earlier: str, later: date) -> int:
    """Whole days from the ISO date ``earlier`` to ``later``."""
    return (later - date.fromisoformat(earlier)).days


def file_name(record: str) -> str:
    """The last segment of a record ID."""
    return record.rsplit("/", 1)[-1]


_EXTS = re.compile(r"(\.(pdf|eml|docx|xlsx|pptx|html?|md|markdown|txt|json|csv|tsv|xml))+$", re.I)


def stem(name: str) -> str:
    """A file name without its extensions: ``Bench Results.xlsx.txt`` is ``Bench Results``."""
    return _EXTS.sub("", file_name(name))


def name_key(name: str) -> str:
    """A name's words, folded: ``Bench Results.xlsx`` and ``bench-results.csv`` match."""
    return " ".join(re.findall(r"[a-z0-9]+", stem(name).lower()))


def slug(text: str) -> str:
    """A key: lower case letters and digits, other runs as one ``-``."""
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")


UNIT_WORDS = (
    "ms", "milliseconds?", "s", "secs?", "seconds?", "min", "mins?", "minutes?", "h", "hrs?",
    "hours?", "days?", "nights?", "weeks?", "months?", "years?", "mm", "cm", "m", "km", "µm",
    "mg", "g", "kg", "N·m", "Nm", "N", "kN", "mA", "A", "mV", "V", "W", "kW", "mW", "Hz", "kHz",
    "MHz", "dB", "%", "°C", "°F", "K", "bpm", "rpm", "Pa", "kPa", "bar", "L", "ml", "mL",
    "degrees?", "percent", "g RMS", "% VWC", "VWC", "m3/m3",
)  # fmt: skip
"""Units of measure as specifications write them (SI symbols, time words, percentages)."""
_QTY = re.compile(
    r"(?<![\w.])(-?\d+(?:[.,]\d+)?)(?:-|\s*)(?:(?:consecutive|full|whole|more|extra|"
    r"additional)\s+)?(" + "|".join(sorted(UNIT_WORDS, key=len, reverse=True)) + r")(?![\w])"
)  # fmt: skip


def quantity(text: str) -> tuple[int, str, str] | None:
    """The first number in ``text`` written with a unit: where it starts, its value, its unit.

    The unit is a unit of measure (:data:`UNIT_WORDS`), with a word between allowed
    (``6 consecutive hours``); a date's day (``2 June``) or a count of things is no quantity.
    """
    m = _QTY.search(text)
    if m is None:
        return None
    return m.start(), m.group(1), m.group(2)


def quantities(text: str) -> list[tuple[int, int]]:
    """Every quantity's range in ``text`` (:func:`quantity`)."""
    return [m.span() for m in _QTY.finditer(text)]


def _line_of(raw: str, start: int, end: int) -> tuple[int, int]:
    s = raw.rfind("\n", 0, start) + 1
    e = raw.find("\n", end)
    return s, len(raw) if e < 0 else e


def passages(found: ReturnedText, fmt: str) -> tuple[list[Segment], list[SpanDraft]]:
    """The record's segments, and one draft per segment widened to its whole lines.

    A text's heading or list item is shown with its marks (``## Mechanism``, ``1. ...``),
    as a reader sees the document; an HTML block or a JSON record is shown as it is (a
    unit of a JSON record of records, :func:`record_units`, as its own text).
    """
    raw = found.text
    segs = [Segment("data", 0, len(raw), raw)] if fmt == "json" else list(segment(raw, fmt))
    out = []
    for s in segs:
        a, b = (s.start, s.end) if fmt in ("html", "json") else _line_of(raw, s.start, s.end)
        if fmt == "xml" and s.attrs.get("key"):  # an element's text, shown with its tag
            tag = raw.rfind("<" + s.attrs["key"], max(0, a - 200), s.start)
            a = _line_of(raw, tag, tag)[0] if tag >= 0 else a
        if s.kind == "paragraph" and fmt not in ("html", "json"):
            a, b = _line_of(raw, s.start, s.start)[0], _line_of(raw, s.end - 1, s.end)[1]
        out.append(SpanDraft(found.source, found.record_id, found.version, a, b))
    return segs, out


_LABEL = re.compile(r"^[A-Z]{1,3}\d{1,3}$")
"""An element label a document gives a step itself (``E2``, ``G1``)."""
_MARK_BEFORE = re.compile(
    r"(\(\w{1,3}\)|\b[A-Za-z]{1,3}\d{0,3}[.)]|\b\d{1,3}[.)]|\b[A-Z]{1,3}\d{1,3}:?)\s*$"
)
_LEADING_LABEL = re.compile(r"^([A-Z]{1,3}\d{1,3})(?::|\.|\s{2,})\s*")


def step_marker(raw: str, segs: Sequence[Segment], start: int, text: str) -> str:
    """The mark a step is written with: its list marker, or the label right before it.

    ``(b2)``, ``E2.``, ``3)``, or a label leading its own text (``A1: ...``); "" when the
    step is written as plain sentences.
    """
    seg = next((g for g in segs if g.start <= start < g.end or g.start == start), None)
    mark = seg.attrs.get("marker", "") if seg is not None else ""
    if not mark or mark in "-*+•▪◦":
        line_start = raw.rfind("\n", 0, start) + 1
        before = _MARK_BEFORE.search(raw[line_start:start])
        mark = before.group(1) if before else ""
    if not mark or mark in "-*+•▪◦":
        lead = _LEADING_LABEL.match(text.strip())
        mark = lead.group(1) if lead else ""
    return mark.strip().strip("().:").strip()


def _mark_key(mark: str) -> tuple[int, str, int]:
    m = re.fullmatch(r"([A-Za-z]*)(\d*)", mark)
    if m is None:
        return (2, mark, 0)
    letters, digits = m.group(1).lower(), m.group(2)
    if letters and not digits:
        return (0, letters, 0)
    if letters:
        return (0, letters, int(digits))
    return (1, "", int(digits or 0))


def label_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each step its element label, in place: the document's own, else ``S1``...

    A step written with its own label (``E2``, ``G1``) keeps it. Steps written with list
    marks are numbered in the order of their marks, so a step added later between two
    others (``(b2)`` after ``(b)``) takes its place in the sequence; plain steps are
    numbered in the order they are written.
    """
    marks = [str(s.get("marker", "")) for s in steps]
    if marks and all(_LABEL.match(m) for m in marks) and len(set(marks)) == len(marks):
        for s, m in zip(steps, marks, strict=True):
            s["label"] = m
        return steps
    if all(marks) and len(set(marks)) == len(marks):
        order = sorted(range(len(steps)), key=lambda i: _mark_key(marks[i]))
    else:
        order = list(range(len(steps)))
    for rank, i in enumerate(order, start=1):
        steps[i]["label"] = f"S{rank}"
    steps.sort(key=lambda s: int(str(s["label"])[1:]) if str(s["label"])[1:].isdigit() else 0)
    return steps


def step_date(raw: str, start: int, end: int, record_date: Any) -> dict[str, Any] | None:
    """A date written on a step's own line, when it differs from the record's date.

    A notebook's margin note (``[margin, added 2 Apr 2026]``) dates the step it adds;
    the step keeps that date as its own record, never the page's.
    """
    a = raw.rfind("\n", 0, start) + 1
    b = raw.find("\n", end)
    line = raw[a : len(raw) if b < 0 else b]
    found = find_dates(line, "dmy")
    if not found:
        return None
    iso, ds, de = found[0]
    if record_date is not None and iso == str(record_date):
        return None
    note = line.strip()
    for m in re.finditer(r"\[[^\[\]]*\]|\([^()]*\)", line):
        if m.start() <= ds and de <= m.end():
            note = m.group(0)  # the bracketed note that dates it
            break
    return {"date": iso, "date_as_written": line[ds:de], "line": note}


def _iso_dates(text: str) -> set[str]:
    """Every date ``text`` writes, as ISO."""
    return {iso for iso, _, _ in find_dates(text, "dmy")}


def _section_after(raw: str, segs: Sequence[Segment], at: int) -> str:
    """The text under the heading at ``at``, up to the next heading of its level or above."""
    head = next((g for g in segs if g.kind == "heading" and g.start <= at < g.end + 1), None)
    if head is None:
        return ""
    nxt = next((g.start for g in segs if g.kind == "heading" and g.start > head.end
                and g.level <= head.level), len(raw))  # fmt: skip
    return raw[head.end : nxt]


_ID_TOKEN = re.compile(r"(?<![\w-])([A-Z][A-Z0-9]{0,7}(?:-[A-Z][A-Z0-9]{0,7})*-\d{1,6})(?![\w-])")
"""An identifier as ventures write one for an idea or a record (``XX-123``, ``SYN-XX-12``)."""


def id_tokens(text: str) -> list[str]:
    """The identifiers ``text`` names, in order, each once."""
    return list(dict.fromkeys(_ID_TOKEN.findall(text)))


def _brackets(raw: str) -> dict[int, tuple[int, int]]:
    """Each JSON object's ``{`` offset: its end and its parent container's ``{`` or ``[``."""
    out: dict[int, tuple[int, int]] = {}
    stack: list[int] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == '"':
            i += 1
            while i < n and raw[i] != '"':
                i += 2 if raw[i] == "\\" else 1
        elif ch in "{[":
            stack.append(i)
        elif ch in "}]" and stack:
            opened = stack.pop()
            if raw[opened] == "{":
                out[opened] = (i + 1, stack[-1] if stack else -1)
        i += 1
    return out


def record_units(
    raw: str, fmt: str, segs: Sequence[Segment], sections: bool = True
) -> list[tuple[int, int]]:
    """The units a record may hold one thing each in, as raw ranges, by structure alone.

    A JSON value: each object of its largest list of objects (a search export's hits, a
    tracker's issues). XML: each element that repeats under one parent. A text: its parts
    between rule lines when it has two or more, else its sections (``record_sections``).
    A record with fewer than two units is one unit. With ``sections`` false a text's
    headings do not cut it: a note's sections are parts of one thing, its rule lines are
    not. What a unit holds is the extraction's answer; which units are one thing is
    :meth:`SourceWorld.candidates`' join.
    """
    whole = [(0, len(raw))]
    if fmt in ("json", "jsonl"):
        objs = _brackets(raw)
        by_parent: dict[int, list[tuple[int, int]]] = {}
        for start, (end, parent) in objs.items():
            if (parent >= 0 and raw[parent] == "[") or (fmt == "jsonl" and parent < 0):
                by_parent.setdefault(parent, []).append((start, end))
        best = max(by_parent.values(), key=len, default=[])
        return sorted(best) if len(best) >= 2 else whole
    if fmt == "xml":
        heads = [g for g in segs if g.kind == "heading"]
        levels = [g.level for g in heads]
        level = next((lv for lv in sorted(set(levels)) if levels.count(lv) >= 2), None)
        starts = [raw.rfind("\n", 0, g.start) + 1 for g in heads if g.level == level]
        if len(starts) < 2:
            return whole
        ends = [*starts[1:], len(raw)]
        return list(zip(starts, ends, strict=True))
    rules = [raw.rfind("\n", 0, g.start) + 1 for g in segs if g.kind == "rule"]
    if len(rules) >= 2:
        bounds = [0, *rules, len(raw)]
        return [(a, b) for a, b in itertools.pairwise(bounds) if raw[a:b].strip()]
    parts = record_sections(list(segs)) if sections else []
    if len(parts) < 2:
        return whole
    starts = [raw.rfind("\n", 0, segs[a].start) + 1 if a < len(segs) else len(raw)
              for a, _ in parts]  # fmt: skip
    starts[0] = 0
    return list(zip(starts, [*starts[1:], len(raw)], strict=True))


@dataclass(frozen=True)
class Read:
    """An extraction's accepted answers over one record, with each value's record ranges."""

    values: Mapping[str, Any]
    ranges: Mapping[str, tuple[tuple[int, int], ...]]

    def text(self, raw: str, name: str) -> str:
        """The first span's raw text of a single field ("" when unknown)."""
        r = self.ranges.get(name, ())
        return raw[r[0][0] : r[0][1]] if r else ""

    def items(self, raw: str, name: str) -> list[tuple[str, int, int]]:
        """Each item of a list field: its raw text and range."""
        return [(raw[s:e], s, e) for s, e in self.ranges.get(name, ())]


def accepted(judged: Sequence[Judgement], drafts: Sequence[SpanDraft]) -> Read:
    """The accepted judgements as values and record ranges; abstentions are left out."""
    values: dict[str, Any] = {}
    ranges: dict[str, tuple[tuple[int, int], ...]] = {}
    for j in judged:
        if not j.accepted or j.value is None:
            continue
        got = [d for d in (span_draft(ref, drafts) for ref in j.spans) if d is not None]
        values[j.name] = j.value
        ranges[j.name] = tuple((d.start, d.end) for d in got)
    return Read(values, ranges)


@dataclass
class Table:
    """One table among the inputs: its record, version, text, columns, roles and rows.

    A row is ``{column: value}`` plus ``_start`` and ``_end`` (its raw range) and
    ``_cells`` (each column's raw range).
    """

    record: str
    version: str
    text: str
    header: list[str]
    roles: dict[str, str]
    rows: list[dict[str, Any]]
    part_column: str | None = None

    def get(self, row: Mapping[str, Any], role: str) -> str:
        """The row's value for ``role``; empty when the table has no such column."""
        col = self.roles.get(role)
        return "" if col is None else str(row.get(col, ""))

    def cell_end(self, row: Mapping[str, Any], role: str) -> int | None:
        """Where the row's ``role`` cell ends in the raw text."""
        col = self.roles.get(role)
        cells = row.get("_cells", {})
        return None if col is None or col not in cells else int(cells[col][1])


def _pipe_tables(found: ReturnedText) -> list[tuple[list[str], list[dict[str, Any]]]]:
    """Every pipe table inside a text record: its header and rows with raw ranges."""
    out: list[tuple[list[str], list[dict[str, Any]]]] = []
    header: list[str] | None = None
    rows: list[dict[str, Any]] = []
    for s in segment(found.text, sniff_format(found.record_id, found.text)):
        if s.kind == "header":
            if header is not None and rows:
                out.append((header, rows))
            header, rows = [c[0] for c in s.cells], []
        elif s.kind == "row" and header is not None:
            cells = {header[i]: c for i, c in enumerate(s.cells) if i < len(header)}
            row: dict[str, Any] = {k: v[0] for k, v in cells.items()}
            row["_start"], row["_end"] = s.start, s.end
            row["_cells"] = {k: (v[1], v[2]) for k, v in cells.items()}
            rows.append(row)
        elif s.kind not in ("row", "header") and header is not None and rows:
            out.append((header, rows))
            header, rows = None, []
    if header is not None and rows:
        out.append((header, rows))
    return out


_YES = frozenset({"yes", "y", "true", "public", "1"})
_NO = frozenset({"no", "n", "false", "private", "0"})


def yes_no(value: str) -> str:
    """A yes-or-no cell as ``yes`` or ``no`` (``true``, ``Y``, ``public``); else as written."""
    v = value.strip().lower()
    return "yes" if v in _YES else "no" if v in _NO else value.strip()


_KV_LINE = re.compile(r"^([A-Z][A-Za-z][A-Za-z ]{0,24}):[ \t]*(.*?)[ \t]*$")


def _key_value_tables(raw: str) -> list[tuple[list[str], list[dict[str, Any]]]]:
    """Records written as blocks of ``Key: value`` lines, one block per record.

    A register kept as text (``Event: ...``, ``Date: ...``, a blank line, the next block):
    a table when two or more blocks share at least three keys. An empty value stays empty.
    """
    blocks: list[list[tuple[str, int, int, int]]] = [[]]
    at = 0
    for line in raw.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        m = _KV_LINE.match(body)
        if m:
            blocks[-1].append((m.group(1).strip(), at + m.start(2), at + m.end(2), at))
        elif blocks[-1]:
            blocks.append([])
        at += len(line)
    blocks = [b for b in blocks if len(b) >= 2]
    if len(blocks) < 2:
        return []
    keys = [k for b in blocks for k, *_ in b]
    shared = [k for k in dict.fromkeys(keys) if keys.count(k) >= 2]
    if len(shared) < 3:
        return []
    shared = list(dict.fromkeys(keys))  # every key a block holds is a column
    rows = []
    for b in blocks:
        row: dict[str, Any] = {k: raw[s:e] for k, s, e, _ in b}
        row["_start"], row["_end"] = b[0][3], b[-1][2]
        row["_cells"] = {k: (s, e) for k, s, e, _ in b}
        rows.append(row)
    return [(shared, rows)]


class OneRecord:
    """A record reader holding one text: a fetched web page, read as the inputs are.

    The publication and document views of :class:`SourceWorld` read through it, so a page
    fetched by URL is recognised and read by the same rules as an uploaded file.
    """

    def __init__(self, text: ReturnedText) -> None:
        """Serve ``text`` under its own record ID."""
        self.text = text

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        """The text, when ``record_id`` names it."""
        return self.text if record_id == self.text.record_id else None

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        """The one record ID."""
        return (self.text.record_id,)

    def serves(self, source: str) -> bool:
        """Whether ``source`` is the one text's source."""
        return source == self.text.source


@dataclass
class SourceWorld:
    """The inputs, read as typed views through the runtime's record reader (module doc).

    ``today`` is the date elapsed days are counted to; the run's recorded start date when
    not given (``ToolContext.run_date``). ``models`` is the call's fast-model handle; with
    none, nothing is recognised and every view is empty.
    """

    today: date | None = None
    reader: RecordReader | None = None
    models: ToolModels | None = None
    names: tuple[str, ...] = ()
    """Every input's record ID, when the reader holds only some of them (a model that read
    some inputs knows the others' names from a listing): what a reference is matched against."""
    _cache: dict[str, ReturnedText] = field(default_factory=dict)
    _asked: dict[tuple[Any, ...], Any] = field(default_factory=dict)

    def over(
        self,
        reader: RecordReader | None,
        run_date: str | None = None,
        models: ToolModels | None = None,
    ) -> SourceWorld:
        """This world reading through ``reader``, counting days to ``today`` or the run date."""
        today = self.today or (date.fromisoformat(run_date) if run_date else None)
        return SourceWorld(today=today, reader=reader, models=models)

    # --- raw access -------------------------------------------------------------------

    def _reader(self) -> RecordReader:
        if self.reader is None:
            raise NotInWorld("no inputs are configured")
        return self.reader

    def records(self) -> list[str]:
        """Every input's record ID, sorted."""
        return sorted(self._reader().records(SOURCE_NAME, COLLECTION))

    def found(self, record: str) -> ReturnedText:
        """The record's text and version."""
        if record not in self._cache:
            got = self._reader().read(SOURCE_NAME, record)
            if got is None:
                raise NotInWorld(f"{record} is not among the inputs")
            self._cache[record] = got
        return self._cache[record]

    def raw(self, record: str) -> str:
        """The raw text of ``record``."""
        return self.found(record).text

    def fmt(self, record: str) -> str:
        """The record's format, from its content."""
        return sniff_format(record, self.raw(record))

    def all_names(self) -> list[str]:
        """Every input's record ID: :attr:`names` when given, else the reader's records."""
        return sorted(set(self.names) | set(self.records()))

    def resolve(self, ref: str) -> str | None:
        """The input a reference names (a path, a file name, with or without its folder)."""
        ref = ref.strip().strip("/")
        if not ref or ref.startswith(("http://", "https://")):
            return None
        exact = next((r for r in self.all_names() if r == ref or r.endswith("/" + ref)), None)
        if exact is not None:
            return exact
        key = name_key(ref)
        if not key or not re.search(r"[./]", ref):
            return None
        return next((r for r in self.all_names() if name_key(r) == key), None)

    def exists_anywhere(self, name: str) -> bool:
        """Whether an input's name is ``name``'s words (any extension or separators)."""
        key = name_key(name)
        return bool(key) and any(name_key(r) == key for r in self.all_names())

    def holding(self, ref: str, besides: str) -> str | None:
        """The one other input whose text names ``ref`` in its head (a document's own reference).

        An agreement exported from a word processor carries its reference in its title
        line, not in its file name.
        """
        pattern = re.compile(rf"(?<![\w-]){re.escape(ref)}(?![\w-])")
        found = [r for r in self.records() if r != besides
                 and pattern.search("\n".join(self.raw(r).splitlines()[:8]))]  # fmt: skip
        return found[0] if len(found) == 1 else None

    def file_naming(self, ref: str) -> bool:
        """Whether any input's name holds ``ref``'s words (a referenced document)."""
        key = name_key(ref)
        return bool(key) and any(key in name_key(r) for r in self.all_names())

    # --- asking the aliases -------------------------------------------------------------

    def kind(self, record: str) -> str:
        """The record's kind, by the decide alias over its structural preview (ADR-P07 G3).

        ``other`` when the decision abstains.
        """
        slot = (DECIDE, record)
        if slot not in self._asked:
            found = self.found(record)
            fmt = self.fmt(record)
            try:
                segs = list(segment(found.text, fmt))
            except ValueError:
                segs = list(segment(found.text, "text"))
            state = document_preview(record, fmt, found.text, segs)
            self._asked[slot] = decide_kind(self.models, DECIDE, KINDS, state)[0]
        kind: str = self._asked[slot]
        return kind

    def kinds(self) -> list[dict[str, str]]:
        """Every input with its format and kind."""
        return [{"record": r, "format": self.fmt(r), "kind": self.kind(r)} for r in self.records()]

    def _extract(
        self, alias: str, record: str, unit: tuple[tuple[int, int], ...] | None = None
    ) -> tuple[Read, list[Segment]]:
        """``alias``'s reading of ``record``, or of the passages inside ``unit``'s ranges."""
        slot = (alias, record) if unit is None else (alias, record, unit)
        if slot not in self._asked:
            found = self.found(record)
            fmt = self.fmt(record)
            segs, drafts = passages(found, fmt)
            if unit is not None and fmt == "json":  # a record of records: each its own text
                drafts = [SpanDraft(found.source, found.record_id, found.version, a, b)
                          for a, b in unit]  # fmt: skip
            elif unit is not None:
                drafts = [d for d in drafts if any(a <= d.start < b for a, b in unit)]
            judged = (
                self.models.extract(alias, found, drafts)
                if self.models is not None and drafts
                else ()
            )
            self._asked[slot] = (accepted(judged, drafts), segs)
        read, segs = self._asked[slot]
        return read, segs

    def _choose(self, alias: str, state: str, questions: list[QuestionSpec]) -> dict[str, str]:
        slot = (alias, json.dumps([state, [(q.name, q.prompt, q.options) for q in questions]]))
        if slot not in self._asked:
            judged = self.models.classify(alias, state, questions) if self.models else ()
            self._asked[slot] = {
                j.name: str(j.value) for j in judged if j.accepted and isinstance(j.value, str)
            }
        got: dict[str, str] = self._asked[slot]
        return got

    def candidate_of(self, text: str, cands: Sequence[Mapping[str, Any]]) -> str | None:
        """The candidate ``text`` is about: one whose ID it holds, else ``candidate_of``."""
        if not text.strip() or not cands:
            return None
        ids = [str(c["candidate"]) for c in cands]
        named = [c for c in ids if re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", text)]
        if len(named) == 1:
            return named[0]
        state = (
            "text: "
            + text
            + "\ncandidates:\n"
            + "\n".join(f"- {c['candidate']}: {c['title']}" for c in cands)
        )
        q = QuestionSpec("candidate", "Which candidate is this text about?", "choice",
                         (*ids, "none"))  # fmt: skip
        got = self._choose("candidate_of", state, [q]).get("candidate")
        return None if got in (None, "none") else got

    # --- tables -------------------------------------------------------------------------

    def _roles(self, header: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
        lines = ["columns and sample values:"]
        for c in header:
            sample = [str(r.get(c, "")) for r in rows[:8] if str(r.get(c, ""))]
            lines.append(f"- {c}: " + " | ".join(v[:80] for v in sample))
        qs = [QuestionSpec(f"c{i}", f"What does the column {c!r} hold?", "choice", ROLES)
              for i, c in enumerate(header)]  # fmt: skip
        got = self._choose("table_roles", "\n".join(lines), qs)
        out: dict[str, str] = {}
        for i, c in enumerate(header):
            role = got.get(f"c{i}")
            if role and role != "other" and role not in out:
                out[role] = c
        people = "name" in out or "person_id" in out
        if people and "contributed" not in out and "content_shown" in out and "date" not in out:
            out["contributed"] = out.pop("content_shown")  # what a person did, in a people table
        if "name" in out and "person_id" not in out and "record_id" in out:
            out["person_id"] = out.pop("record_id")  # a table of people: its ID is the person's
        return out

    def _tables(self) -> list[Table]:
        out = []
        for r in self.records():
            if self.kind(r) not in TABLE_KINDS:
                continue
            found = self.found(r)
            fmt = self.fmt(r)
            if fmt in ("csv", "json", "jsonl", "yaml"):
                try:
                    rows_ = read_rows(found.text, fmt)
                except ValueError:
                    continue
                header = list(rows_.columns)
                rows = []
                for row in rows_.rows:
                    d: dict[str, Any] = {c: v for c, (v, _, _) in row.cells.items()}
                    d["_start"], d["_end"] = row.start, row.end
                    d["_cells"] = {c: (s, e) for c, (_, s, e) in row.cells.items()}
                    rows.append(d)
                out.append(Table(r, found.version, found.text, header, self._roles(header, rows),
                                 rows))  # fmt: skip
            elif fmt in ("markdown", "text", "email", "html"):
                for header, rows in [*_pipe_tables(found), *_key_value_tables(found.text)]:
                    out.append(Table(r, found.version, found.text, header,
                                     self._roles(header, rows), rows))  # fmt: skip
        return out

    def contributor_tables(self) -> list[Table]:
        """Tables with a person (an ID or a name) and what each contributed."""
        return [
            t
            for t in self._tables()
            if ("person_id" in t.roles or "name" in t.roles) and "contributed" in t.roles
        ]

    def history_tables(self) -> list[Table]:
        """Tables with a date and what was shown."""
        return [t for t in self._tables() if "date" in t.roles and "content_shown" in t.roles]

    def _person_id(self, t: Table, row: Mapping[str, Any]) -> str:
        pid = t.get(row, "person_id")
        return pid or ("P-" + slug(t.get(row, "name")).lower() if t.get(row, "name") else "")

    def _rows_of(self, t: Table, candidate: str) -> list[dict[str, Any]]:
        cands = self.candidates()["candidates"]
        out = []
        for r in t.rows:
            cell = t.get(r, "candidate")
            if cell == candidate:
                out.append(r)
                continue
            text = cell or t.get(r, "contributed") or t.get(r, "content_shown")
            if cell and any(str(c["candidate"]) == cell for c in cands):
                continue
            if self.candidate_of(text, cands) == candidate:
                out.append(r)
        return out

    def _part(self, t: Table, candidate: str, rows: Sequence[Mapping[str, Any]]) -> tuple[Any, ...]:
        """The record part a view of ``rows`` reads: the candidate's rows by its column."""
        column = t.roles.get("candidate")
        if column is not None and all(t.get(r, "candidate") == candidate for r in rows):
            return (t.record, column, candidate)
        return (t.record, None, None)

    # --- invention notes ----------------------------------------------------------------

    def _units(self, record: str, sections: bool = True) -> list[tuple[int, int]]:
        found = self.found(record)
        fmt = self.fmt(record)
        segs, _ = passages(found, fmt)
        return record_units(found.text, fmt, segs, sections)

    def _groups(self, record: str) -> list[tuple[tuple[int, int], ...]]:
        """The record's units joined into one group per thing they are about.

        A unit whose head (its first lines) names an identifier starts a group, or joins
        the group of the same identifier (a notebook's later page on the same idea); a unit
        whose head names none continues the group above it. With one group, the record is
        read whole.
        """
        raw = self.raw(record)
        groups: dict[str, list[tuple[int, int]]] = {}
        order: list[str] = []
        current = ""
        for a, b in self._units(record, sections=False):
            head = "\n".join(ln for ln in raw[a:b].splitlines() if ln.strip())
            head = "\n".join(head.splitlines()[:3])
            ids = id_tokens(head)
            key = ids[0] if ids else (current or f"@{a}")
            if key not in groups:
                groups[key], current = [], key
                order.append(key)
            elif ids:
                current = key
            groups[key].append((a, b))
        if len(order) < 2:
            return [((0, len(raw)),)]
        return [tuple(groups[k]) for k in order]

    def _named_elsewhere(self, token: str, record: str) -> int:
        """How many other inputs name ``token``."""
        pattern = re.compile(rf"(?<![\w-]){re.escape(token)}(?![\w-])")
        return sum(1 for r in self.records() if r != record and pattern.search(self.raw(r)))

    def _note(
        self, record: str, unit: tuple[tuple[int, int], ...] | None = None
    ) -> tuple[Read, list[Segment]] | None:
        """The record's (or one unit group's) invention reading, when it describes one.

        At least one step of how it works is enough (one paragraph of a plain note).
        """
        kind = self.kind(record)
        tabular = self.fmt(record) in ("csv", "json", "jsonl", "yaml")
        if kind not in READ_AS_NOTE or (kind == OTHER_KIND and tabular):
            return None  # a table no kind fits (an export) is no invention note
        whole = unit is None or unit == ((0, len(self.raw(record))),)
        read, segs = self._extract("note_structure", record, None if whole else unit)
        if not read.values.get("mechanism_steps"):
            return None
        return read, segs

    def _candidate_id(self, record: str, unit: tuple[tuple[int, int], ...], read: Read) -> str:
        """The candidate's ID: the one the extraction read, or one other inputs also use.

        A tracker export gives an idea its own ticket key beside the ID the venture uses
        everywhere else; when the extracted ID is named by no other input and another ID in
        the same text is, the shared one names the candidate.
        """
        raw = self.raw(record)
        given = read.text(raw, "candidate_id").strip().strip(",.")
        text = "\n".join(raw[a:b] for a, b in unit)
        tokens = id_tokens(text)
        if given and given not in tokens:
            tokens.insert(0, given)
        counted = [(self._named_elsewhere(t, record), -i, t) for i, t in enumerate(tokens)]
        best = max(counted, default=(0, 0, ""))
        if given and (self._named_elsewhere(given, record) or best[0] == 0):
            return given
        return best[2] if best[0] > 0 else given

    def candidates(self) -> dict[str, Any]:
        """Every invention candidate's ID, record and title (the portfolio).

        A record may hold several candidates (a notebook's pages, a tracker's ideas): each
        unit group (:meth:`_groups`) is read on its own.
        """
        if ("candidates", "") in self._asked:
            cached: dict[str, Any] = self._asked[("candidates", "")]
            return cached
        out = []
        for r in self.records():
            if self.kind(r) not in READ_AS_NOTE:
                continue
            groups = self._groups(r)
            for unit in groups:
                got = self._note(r, unit)
                if got is None:
                    continue
                read, _ = got
                raw = self.raw(r)
                cid = self._candidate_id(r, unit, read) or slug(stem(r))
                title = read.text(raw, "title").strip() or stem(r)
                row: dict[str, Any] = {"candidate": cid, "note": r, "title": title}
                if len(groups) > 1:
                    row["unit"] = [list(u) for u in unit]
                out.append(row)
        noted = {c["note"] for c in out}
        unread = [r for r in self.records() if r not in noted and self.kind(r) == OTHER_KIND]
        result = {"candidates": sorted(out, key=lambda c: (c["candidate"], c["note"])),
                  "unread": unread}  # fmt: skip
        self._asked[("candidates", "")] = result
        return result

    def note_path(self, candidate: str) -> str:
        """The invention note of ``candidate``."""
        return str(self._candidate_row(candidate)["note"])

    def _candidate_row(self, candidate: str) -> dict[str, Any]:
        for row in self.candidates()["candidates"]:
            if row["candidate"] == candidate:
                return dict(row)
        raise NotInWorld(f"no invention note for {candidate}")

    def _param(self, raw: str, segs: Sequence[Segment], s: int, e: int) -> dict[str, Any]:
        """A parameter quote as ``Parameter``, ``Value`` and ``Unit``, with its row text."""
        seg = next((g for g in segs if g.start <= s and e <= g.end
                    and g.kind == "row"), None)  # fmt: skip
        if seg is not None:
            head = None
            for g in segs:
                if g.kind == "header" and g.start < seg.start:
                    head = g
            names = [c[0] for c in head.cells] if head else []
            row = {names[i] if i < len(names) else f"col{i}": c[0] for i, c in enumerate(seg.cells)}
            line = raw[seg.start : seg.end]
            row_text = line[: line.rfind("|")].rstrip() if line.rstrip().endswith("|") else line
            by = {re.sub(r"[^a-z]+", "_", k.lower()).strip("_"): v for k, v in row.items()}
            values = list(row.values())
            name = next((by[k] for k in by if k in ("parameter", "name", "setting", "item",
                                                     "what")), values[0] if values else "")  # fmt: skip  # noqa: E501
            value = next((by[k] for k in by if k in ("value", "setting_value", "default")),
                         values[1] if len(values) > 1 else "")  # fmt: skip
            unit = next((by[k] for k in by if k in ("unit", "units")), "")
            return {**row, "Parameter": name, "Value": value, "Unit": unit, "row_text": row_text}
        text = raw[s:e]
        parts = re.split(r"\s*(?::|=|\||\.{3,}|\s\u2013\s|\s\u2014\s|\s-\s)\s*", text, maxsplit=1)
        name = parts[0].strip(" *.")
        rest = parts[1].strip(" .") if len(parts) > 1 else ""
        m = re.match(r"^([\d.,]+)\s*([^\d\s][^,;(]*)?", rest)
        value, unit = (m.group(1), (m.group(2) or "").strip()) if m else (rest, "")
        if len(parts) == 1 or not m:
            q = quantity(text)
            if q is not None:  # "window length 30 seconds", "cap the current at 6 mA, not 8"
                at, value, unit = q
                name = text[:at].strip(" ,:") if len(text.split()) <= 8 else name
        return {"Parameter": name, "Value": value, "Unit": unit, "row_text": text}

    def note(self, candidate: str) -> dict[str, Any]:
        """The note's dated record, problem, mechanism, parameters, alternatives, evidence.

        Who contributed comes from the contributor records, never the note's author line.
        """
        row = self._candidate_row(candidate)
        rel = str(row["note"])
        unit = tuple((int(a), int(b)) for a, b in row.get("unit", ())) or None
        got = self._note(rel, unit)
        assert got is not None
        read, segs = got
        raw = self.raw(rel)
        date_r = read.ranges.get("date", ())
        date_line = None
        if date_r:
            a, b = _line_of(raw, date_r[0][0], date_r[0][1])
            date_line = raw[a:b].strip()
        steps = read.items(raw, "mechanism_steps")
        first = steps[0][1] if steps else 0
        heading = next(
            (g.section for g in segs if g.start <= first <= g.end and g.section),
            "",
        )
        if not heading:
            heading = next((g.text for g in reversed(segs) if g.kind == "heading"
                            and g.start < first), "Mechanism")  # fmt: skip
        mechanism = []
        for i, (text, s, e) in enumerate(steps, start=1):
            marker = step_marker(raw, segs, s, text)
            n = int(marker) if marker.isdigit() else i
            step: dict[str, Any] = {"n": n, "text": text.strip(), "marker": marker}
            own = step_date(raw, s, e, read.values.get("date"))
            if own is not None:
                step["dated"] = own
            mechanism.append(step)
        mechanism = label_steps(mechanism)
        refs = sorted({t.strip() for t, _, _ in read.items(raw, "references")})
        return {
            "file": rel,
            "record_id": next(
                (
                    g.attrs.get("value")
                    for g in segs
                    if g.kind == "meta" and g.attrs.get("key") == "record_id"
                ),
                None,
            ),
            "candidate": candidate,
            "title": read.text(raw, "title").strip() or stem(rel),
            "date_line": date_line,
            "date_as_written": read.text(raw, "date").strip() or None,
            "date": read.values.get("date"),
            "problem": read.text(raw, "problem").strip(),
            "mechanism_heading": heading,
            "mechanism_steps": mechanism,
            "parameters": [
                self._param(raw, segs, s, e) for _, s, e in read.items(raw, "parameters")
            ],
            "alternatives": [t.strip() for t, _, _ in read.items(raw, "alternatives")],
            "evidence": read.text(raw, "evidence").strip(),
            "implementation_status": str(read.values.get("implementation_status") or "unknown"),
            "open_points": [t.strip() for t, _, _ in read.items(raw, "open_points")],
            "opinions": [t.strip() for t, _, _ in read.items(raw, "opinions")],
            "references": [{"name": n, "found": self.exists_anywhere(n)} for n in refs],
        }

    # --- tables -------------------------------------------------------------------------

    def contributor_rows(self, candidate: str) -> dict[str, Any]:
        """The contributor records for ``candidate``, without names or employers."""
        keep = ("record_id", "role", "candidate", "contributed", "date", "agreement",
                "source")  # fmt: skip
        rows, parts = [], []
        for t in self.contributor_tables():
            mine = self._rows_of(t, candidate)
            parts.append(self._part(t, candidate, mine))
            for r in mine:
                agreement = t.get(r, "agreement")
                refs = sorted(set(re.findall(r"\b[A-Z][A-Z0-9]*(?:[-/][A-Z0-9]+)*[-/]\d+\b",
                                             agreement)))  # fmt: skip
                documents = []
                for ref in refs:
                    held = self.holding(ref, t.record)
                    documents.append({"ref": ref, "found": self.file_naming(ref) or bool(held),
                                      **({"file": held} if held else {})})  # fmt: skip
                row = {k: t.get(r, k) for k in keep}
                row["person_id"] = self._person_id(t, r)
                row["candidate"] = row["candidate"] or candidate
                when = row["date"]
                row["date"] = day_first(when) or when if when else when
                rows.append({**row, "file": t.record, "agreement_documents": documents})
        return {"file": parts[0][0] if parts else None, "rows": rows, "parts": parts}

    def people(self, person_ids: Sequence[str]) -> dict[str, Any]:
        """Names, employers and roles of ``person_ids``, from the contributor records."""
        found: dict[str, dict[str, str]] = {}
        parts = []
        for t in self.contributor_tables():
            parts.append((t.record, t.roles.get("person_id"), ",".join(sorted(person_ids))))
            for r in t.rows:
                pid = self._person_id(t, r)
                if pid in person_ids and pid not in found:
                    found[pid] = {
                        "person_id": pid,
                        "name": t.get(r, "name"),
                        "employer": t.get(r, "employer"),
                        "role": t.get(r, "role"),
                    }
        return {"file": parts[0][0] if parts else None,
                "people": [found[p] for p in sorted(found)], "parts": parts}  # fmt: skip

    def disclosure_rows(self, candidate: str) -> dict[str, Any]:
        """The disclosure history rows for ``candidate``: dates kept as written.

        The audience column and any external link are left out of the view; a linked file
        among the inputs is named so the agent can read it.
        """
        out, parts = [], []
        for t in self.history_tables():
            mine = self._rows_of(t, candidate)
            parts.append(self._part(t, candidate, mine))
            for n, r in enumerate(mine, start=1):
                when = t.get(r, "date")
                keys = ("disclosure_id", "date", "candidate")
                ends = [x for x in (t.cell_end(r, k) for k in keys)
                        if x is not None]  # fmt: skip
                start = int(r["_start"])
                row_start = t.text[start : max(ends)] if ends else t.text[start : int(r["_end"])]
                out.append(
                    {
                        "disclosure_id": t.get(r, "disclosure_id")
                        or t.get(r, "record_id")
                        or f"{slug(stem(t.record))}-{n}",
                        "date": day_first(when) if when else None,
                        "date_as_written": when or None,
                        "event_type": t.get(r, "event_type"),
                        "content_shown": t.get(r, "content_shown"),
                        "public": yes_no(t.get(r, "public")),
                        "by": t.get(r, "by"),
                        "linked_file": self.resolve(t.get(r, "source")),
                        "file": t.record,
                        "row_start": row_start,
                    }
                )
        self._link_write_ups(out)
        out.extend(self._written_events(candidate, parts))
        return {"file": parts[0][0] if parts else None, "rows": out, "parts": parts}

    def _linked_rows(self) -> list[dict[str, Any]]:
        """Every history row's date, as :meth:`_link_write_ups` links write-ups to them."""
        rows = [{"date": day_first(t.get(r, "date")) if t.get(r, "date") else None,
                 "linked_file": self.resolve(t.get(r, "source"))}
                for t in self.history_tables() for r in t.rows]  # fmt: skip
        self._link_write_ups(rows)
        return rows

    def _link_write_ups(self, rows: list[dict[str, Any]]) -> None:
        """A row with no linked file gets the disclosure write-up that is its own, if one is.

        A poster or a talk saved as its own input is the write-up of the register row it
        dates: a disclosure record no row links, whose text holds the row's date. One such
        document per row, never two.
        """
        linked = {self.resolve(t.get(r, "source")) for t in self.history_tables() for r in t.rows}
        tabled = {t.record for t in self._tables()}
        loose = [r for r in self.records() if self.kind(r) == "disclosure_record"
                 and r not in linked and r not in tabled]  # fmt: skip
        for row in rows:
            if row.get("linked_file") or not row.get("date"):
                continue
            dated = [r for r in loose if row["date"] in _iso_dates(self.raw(r))]
            if len(dated) == 1:
                row["linked_file"] = dated[0]

    def _written_events(self, candidate: str, parts: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
        """Disclosure events written as prose.

        A record decided ``disclosure_record`` that is no table and that no table links to
        (a note that says "we posted a video of it on ..."). Each block holding a date is
        one event about the candidate ``candidate_of`` names. Whether it was public stays
        "unknown": the record's words are shown for the general model to judge, never
        decided here.
        """
        linked = {self.resolve(t.get(r, "source")) for t in self.history_tables() for r in t.rows}
        linked |= {r.get("linked_file") for r in self._linked_rows()}
        tabled = {t.record for t in self._tables()}
        cands = self.candidates()["candidates"]
        out: list[dict[str, Any]] = []
        for rel in self.records():
            if self.kind(rel) != "disclosure_record" or rel in linked or rel in tabled:
                continue
            found = self.found(rel)
            fmt = self.fmt(rel)
            if fmt in ("csv", "json"):
                continue
            n = 0
            for seg in segment(found.text, fmt):
                if seg.kind not in ("paragraph", "item", "text"):
                    continue
                text = found.text[seg.start : seg.end]
                dates = find_dates(text, "dmy")
                if not dates or self.candidate_of(text, cands) != candidate:
                    continue
                n += 1
                iso, ds, de = dates[0]
                parts.append((rel, None, None))
                out.append({"disclosure_id": f"{slug(stem(rel))}-{n}", "date": iso,
                            "date_as_written": text[ds:de], "event_type": None,
                            "content_shown": text.strip(), "public": "unknown", "by": None,
                            "linked_file": rel, "file": rel,
                            "row_start": text.strip()})  # fmt: skip
        return out

    def document(self, rel: str) -> dict[str, Any]:
        """A linked disclosure document: an email (headers, body) or a record (sections)."""
        text = self.raw(rel)
        fmt = self.fmt(rel)
        segs = list(segment(text, fmt))
        if fmt == "email":
            head = next((s for s in segs if s.kind == "meta" and s.attrs.get("key") in
                         ("date", "sent")), None)  # fmt: skip
            body_at = text.find("\n\n")
            iso = day_first(head.attrs.get("value", "")) if head else None
            return {
                "file": rel,
                "type": "email",
                "date_header": text[head.start : head.end] if head else None,
                "date": iso,
                "body": text[body_at:].strip() if body_at >= 0 else "",
            }
        sections: dict[str, str] = {}
        current = ""
        texts: dict[str, list[str]] = {}
        for s in segs:
            if s.kind == "heading" and s.level >= 2:
                current = s.text
                continue
            if s.kind in ("paragraph", "item", "row"):
                texts.setdefault(current, []).append(text[s.start : s.end])
        for heading, parts in texts.items():
            if heading:
                key = heading.strip().lower().split()[0]
                sections.setdefault(key, "\n".join(parts))
        meta = {s.attrs.get("key"): s.attrs.get("value") for s in segs if s.kind == "meta"}
        dated = next((day_first(text[s.start : s.end]) for s in segs[:12]
                      if s.kind in ("paragraph", "meta") and day_first(text[s.start : s.end])),
                     None)  # fmt: skip
        return {
            "file": rel,
            "type": "record",
            "disclosure_id": meta.get("disclosure_id"),
            "date": dated,
            "sections": sections,
            "body": "\n".join(p for v in texts.values() for p in v),
        }

    def priorities(self) -> dict[str, Any]:
        """The ranked business priorities, each with the candidate it relates to."""
        cands = self.candidates()["candidates"]
        for rel in self.records():
            if self.kind(rel) != "business_priorities":
                continue
            read, _ = self._extract("priorities", rel)
            raw = self.raw(rel)
            items = read.items(raw, "items")
            if not items:
                continue
            out = []
            segs = list(segment(raw, self.fmt(rel)))
            for n, (text, s0, _e) in enumerate(items, start=1):
                lead = re.match(r"^\s*(\d+)[.)]?\s", text)
                said = re.search(r"\b(?:relates to|for|candidate)\s+([\w.-]+)", text, re.I)
                ids = {str(c["candidate"]) for c in cands}
                rel_to = said.group(1).rstrip(".") if said and said.group(1).rstrip(".") in ids \
                    else self.candidate_of(text, cands)  # fmt: skip
                if rel_to is None:  # a ranked heading: the candidate its section names
                    rel_to = self.candidate_of(_section_after(raw, segs, s0), cands)
                body = text.strip()
                bold = re.match(r"^(\*\*.+?\*\*|__.+?__)", body)
                first = re.match(r"^(.+?[.!?])(?=\s|$)", body)
                lead_m = bold or first
                title = lead_m.group(1) if lead_m is not None else body
                out.append({"rank": int(lead.group(1)) if lead else n, "title": title,
                            "text": body[len(title) :].strip(), "relates_to": rel_to})  # fmt: skip
            return {"file": rel, "items": out}
        return {"file": None, "items": []}

    # --- publications -------------------------------------------------------------------

    def _publication(
        self, rel: str, unit: tuple[tuple[int, int], ...] | None = None
    ) -> Read | None:
        if self.kind(rel) != "prior_art_publication":
            return None
        read, _ = self._extract("publication_biblio", rel, unit)
        return read if read.values.get("number") else None

    def _publication_records(self) -> dict[str, tuple[str, tuple[tuple[int, int], ...] | None]]:
        """Each publication's record, and its unit when one record holds several.

        A search export or a search report lists its hits as records or sections: each unit
        of the record (:func:`record_units`) that states a publication number is one.
        """
        if ("publications", "") in self._asked:
            cached: dict[str, Any] = self._asked[("publications", "")]
            return cached
        out: dict[str, tuple[str, tuple[tuple[int, int], ...] | None]] = {}
        for rel in self.records():
            if self.kind(rel) != "prior_art_publication":
                continue
            units = self._units(rel)
            if len(units) > 1:
                each: dict[str, tuple[str, tuple[tuple[int, int], ...] | None]] = {}
                for u in units:
                    read = self._publication(rel, (u,))
                    if read is not None:
                        each.setdefault(read.text(self.raw(rel), "number").strip(), (rel, (u,)))
                if len(each) >= 2:  # several publications in one record, one per unit
                    for k, v in each.items():
                        out.setdefault(k, v)
                    continue
            read = self._publication(rel)
            if read is not None:
                out.setdefault(read.text(self.raw(rel), "number").strip(), (rel, None))
        self._asked[("publications", "")] = out
        return out

    def publication_ids(self) -> list[str]:
        """Every publication's number, from its own content."""
        return sorted(self._publication_records())

    def publication(self, pub: str) -> dict[str, Any]:
        """One publication: numbers, dates, URL, status wordings verbatim, claims, text.

        The applicant is not part of the view. A field the source does not state is None.
        """
        found = self._publication_records().get(pub)
        if found is None:
            raise NotInWorld(f"no publication {pub}")
        rel, unit = found
        read = self._publication(rel, unit)
        assert read is not None
        raw = self.raw(rel)
        dates = {
            k: read.values.get(f)
            for k, f in (("priority", "priority_date"), ("filing", "filing_date"),
                         ("publication", "publication_date"), ("grant", "grant_date"))
            if read.values.get(f)
        }  # fmt: skip
        priority = []
        for text, _s, _e in read.items(raw, "priority_claims"):
            number = re.search(r"[A-Z][A-Z0-9]*(?:[-/ ][A-Z0-9]+)*[-/]\d[\w/-]*", text)
            when = find_dates(text, "dmy")
            relation = re.search(
                r"\b(continuation-in-part of|continuation of|divisional of|"
                r"continuation|divisional)\b",
                text,
                re.I,
            )
            if number:
                claim: dict[str, Any] = {"number": number.group(0), "date": when[0][0]
                                         if when else None}  # fmt: skip
                if relation:
                    claim["relation"] = relation.group(1).lower()
                priority.append(claim)
        status = []
        for text, s, _e in read.items(raw, "status"):
            line_s, line_e = _line_of(raw, s, s)
            line = raw[line_s:line_e]
            register = bool(re.search(r"register|legal status", line, re.I))
            as_of = find_dates(line, "dmy")
            status.append({"wording": text, "source": "register extract" if register else
                           "document", "as_of": as_of[0][0] if as_of and register else None})  # fmt: skip  # noqa: E501
        claims = []
        for i, (text, _s, _e) in enumerate(read.items(raw, "claims"), start=1):
            body = text.strip()
            lead = re.match(r"^(\d+)\.\s+", body)
            dep = re.search(r"\b(?:of|in|according to|as claimed in) claim (\d+)\b", body)
            claims.append(
                {
                    "number": int(lead.group(1)) if lead else i,
                    "text": body[lead.end() :] if lead else body,
                    "depends_on": int(dep.group(1)) if dep else None,
                }
            )
        url = read.text(raw, "url").strip() or None
        return {
            "file": rel,
            "id": pub,
            "office": read.text(raw, "office").strip() or None,
            "kind_code": read.text(raw, "kind_code").strip() or None,
            "title": read.text(raw, "title").strip() or None,
            "dates": dates,
            "priority_claims": priority,
            "url": url,
            "status": status,
            "abstract": read.text(raw, "abstract").strip(),
            "description": read.text(raw, "description").strip(),
            "claims": claims,
        }

    def search(self, groups: Sequence[Sequence[str]]) -> list[dict[str, Any]]:
        """Publications whose text matches every group (any one term of a group).

        The text is the title, abstract, description and claims, and the numbers of the
        priorities it claims: a search by a priority number finds that family's members.
        """
        hits = []
        for pub in self.publication_ids():
            p = self.publication(pub)
            hay = " ".join(
                [
                    p["title"] or "",
                    p["abstract"],
                    p["description"],
                    *(c["text"] for c in p["claims"]),
                    *(str(c["number"]) for c in p["priority_claims"]),
                ]
            ).casefold()
            if all(any(t.casefold() in hay for t in g) for g in groups):
                hits.append({"id": p["id"], "title": p["title"]})
        return hits
