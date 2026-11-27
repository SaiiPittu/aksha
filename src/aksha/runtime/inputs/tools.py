"""The harness's input tools: list, read and search the inputs, the same for every agent.

ADR-P10 amendment T1. A document agent reads whatever the person uploaded or configured
(saved pages, notes, exports, emails, text the ingest extension converted from a PDF, a
deck or a spreadsheet). These three tools are how any agent reads them, so no agent writes
its own reader over a folder layout, a file name or a column name. An agent lists them with
:func:`input_tools`, naming its versioned source and, when it has one, its decide alias and
document kinds (:class:`InputReading`, declared data):

``list_inputs(kinds?, formats?, offset?, limit?)``
    The inputs with their format, size (characters and lines), version, a table's columns
    and, when the agent
    declares a decide alias, their document kind (ADR-P07 G3) and the kinds of their
    sections (G4). A recorded query (ADR-P05 D6): its version hashes what it listed, so an
    input added later re-executes it. ``as_of`` is the date the run counts to (a recorded
    time read, ADR-P25 B2), so a model counts days to a recorded date. Mints nothing.

``read_document(ref, offset?, limit?, section?, cells?, where?, extract?)``
    One input through its representation (what the ingest extension serves at the input's
    path: the text as given, or a converted document's header and text). The window is
    numbered lines (``offset`` from 1, ``limit`` lines), capped at
    :attr:`InputReading.page_chars` characters and :attr:`InputReading.line_chars` per line;
    a cut window ends with a notice that says how to continue, and an offset past the end is
    an error that says how many lines there are. Every structural block of the window (a
    heading, a paragraph, a list item, a table row; for JSON also the whole value) is a
    *unit*, with the anchor it sits under
    (the nearest heading, a ``[p.N]`` page, a ``[slide N]``, a ``[sheet X]``); in a
    line-oriented format a block whose whole lines differ from it (a heading with its ``#``,
    an item with its mark) is also a ``line`` unit, so either can be quoted. Each unit is
    minted as one citation span, in unit order, so the model cites unit ``n`` by the
    ``n``-th span ID. ``cells`` mints each cell of a table row as a unit too, so a short
    value can be cited whole. ``extract`` runs decide-then-extract (ADR-P07 G3): the decide
    alias names the kind, the kind's extraction alias fills its typed schema, and each
    value is returned with the units that state it, every value range minted as a unit of
    its own; what the fast models left is listed as ``unresolved``. ``where`` reads only the
    header and the rows of a comma-separated table whose ``column`` holds one of ``values``,
    and records that record part as the read (ADR-P05 D6), so a change to another row
    reopens nothing built from these.

``search_inputs(query, ref?, formats?, match?, limit?)``
    Lexical search over every unit of every input (or of one): ranked passages, each with
    its input, anchor, lines and a snippet. A recorded query whose version hashes the set of
    inputs it hit. Mints nothing: a hit is a pointer, and a claim needs the unit read.

:func:`read_view` is the view ``read_document`` returns, over any returned text, so the
generic ``fetch_url`` (``web/tools.py``) shows a fetched page exactly as an input.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import Any

from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.grounding.parts import part_text, part_version, where_part
from aksha.runtime.grounding.segment import (
    FORMATS,
    Segment,
    read_rows,
    segment,
    sniff_format,
    visible,
)
from aksha.runtime.grounding.types import MIN_QUOTE_CHARS, ReturnedText, SpanDraft
from aksha.runtime.models.documents import (
    OTHER_KIND,
    DocumentKind,
    decide_kind,
    document_preview,
    read_document,
    record_sections,
    section_heading,
)
from aksha.runtime.tools.types import (
    Err,
    Ok,
    RetryPolicy,
    ToolContext,
    ToolDecl,
    ToolOutcome,
    ToolSpec,
)

LIST_INPUTS = "list_inputs"
READ_DOCUMENT = "read_document"
SEARCH_INPUTS = "search_inputs"
INPUT_TOOLS = (LIST_INPUTS, READ_DOCUMENT, SEARCH_INPUTS)
"""The names :func:`input_tools` declares, in order."""

CITE_UNIT = (
    'cite unit n as {"cite": {"span": <the n-th ID of citation_spans>, "quote": <its '
    f"words copied exactly, at least {MIN_QUOTE_CHARS} characters, or the whole unit>}}}} "
    "inside the saved field it grounds"
)
"""What a read result says about citing one of its units (ADR-P13 D2)."""

PAGE_LINES = 400
"""Lines a window holds at most by default."""
MAX_LINES = 2_000
"""The largest ``limit`` a call may ask for."""
PAGE_CHARS = 16_000
"""Characters of line text a window holds at most by default."""
LINE_CHARS = 2_000
"""A line longer than this is shown cut, with a notice."""
SNIPPET_CHARS = 200
SLOW_RETRY = RetryPolicy(timeout_ms=600_000, max_retries=1)
"""A read may ask a live fast model per input: seconds each."""
LIST_LIMIT = 100
SEARCH_LIMIT = 20
LINE_FORMATS = frozenset({"markdown", "text", "email", "csv"})
"""Formats whose blocks are also units as whole lines (a heading with its ``#``, an item with
its mark)."""
_PAGE = re.compile(r"^\[(p\.\d+|slide \d+|sheet [^\]\n]{1,80})\]\s*$")
"""A page, slide or sheet marker line of a converted document's representation."""
_WORD = re.compile(r"\w[\w'.-]*\w|\w", re.UNICODE)


@dataclass(frozen=True)
class InputReading:
    """How an agent's input tools read: its source, and how it decides a document's kind.

    ``source`` and ``collection`` name the versioned source the inputs are records of.
    ``decide`` is the agent's decide alias and ``kinds`` its document kinds (ADR-P07 G3);
    with no decide alias, ``list_inputs`` lists no kinds and ``extract`` is refused.
    ``page_lines``, ``page_chars`` and ``line_chars`` bound one ``read_document`` window.
    ``result_chars`` is the agent's result cap (ADR-P08 ``result_cap_chars``): a window whose
    whole result would not fit under it is cut shorter, never left for the cap to cut in
    the middle. ``retry`` is the tools' timeout and
    retry policy: a call that asks a live fast model per input may take seconds each.
    """

    source: str
    collection: str
    decide: str | None = None
    kinds: tuple[DocumentKind, ...] = ()
    page_lines: int = PAGE_LINES
    page_chars: int = PAGE_CHARS
    line_chars: int = LINE_CHARS
    result_chars: int | None = None
    retry: RetryPolicy = SLOW_RETRY

    def aliases(self) -> frozenset[str]:
        """The fast-class aliases the tools may ask: the decide alias and each kind's schema."""
        if self.decide is None:
            return frozenset()
        return frozenset({self.decide, *(k.extract for k in self.kinds if k.extract)})

    def kind_names(self) -> tuple[str, ...]:
        """The declared kinds and :data:`OTHER_KIND`, in order."""
        return (*dict.fromkeys(k.name for k in self.kinds), OTHER_KIND)


@dataclass(frozen=True)
class Unit:
    """One citable passage of a window: ``text[start:end]``, its kind and its anchor."""

    kind: str
    start: int
    end: int
    anchor: str
    field: str | None = None


# --- structure -------------------------------------------------------------------------


def segments_of(text: str, fmt: str) -> list[Segment]:
    """The record's structural segments; plain text when its format will not parse."""
    try:
        return list(segment(text, fmt))
    except ValueError:
        return list(segment(text, "text"))


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for m in re.finditer("\n", text):
        starts.append(m.end())
    return starts


def _line_of(starts: Sequence[int], at: int) -> int:
    """The 1-based line holding offset ``at``."""
    lo, hi = 0, len(starts) - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if starts[mid] <= at:
            lo = mid
        else:
            hi = mid - 1
    return lo + 1


def _widen(text: str, start: int, end: int) -> tuple[int, int]:
    """``[start, end)`` widened to the whole lines it touches, line breaks excluded."""
    a = text.rfind("\n", 0, start) + 1
    b = text.find("\n", max(end - 1, start))
    return a, len(text) if b < 0 else b


def _pages(text: str, starts: Sequence[int]) -> list[tuple[int, str]]:
    """Each page, slide or sheet marker: its line and its label."""
    out = []
    for n, s in enumerate(starts, start=1):
        e = text.find("\n", s)
        m = _PAGE.match(text[s : len(text) if e < 0 else e])
        if m is not None:
            out.append((n, m.group(1)))
    return out


def _anchor(line: int, section: str, pages: Sequence[tuple[int, str]]) -> str:
    page = next((label for at, label in reversed(pages) if at <= line), "")
    head = " ".join(section.split())[:80]
    return " / ".join(x for x in (page, head) if x)


def units_of(text: str, fmt: str, segs: Sequence[Segment], *, cells: bool = False) -> list[Unit]:
    """The record's units in document order (module doc): blocks, then cells when asked.

    A JSON value is first one unit whole, since its scanner reads only its largest list of
    objects. A block that runs across a page, slide or sheet marker is also each piece
    between markers, so a quote is cited under its own page. Two units over the same range
    are one.
    """
    starts = _line_starts(text)
    pages = _pages(text, starts)
    out: list[Unit] = []
    seen: set[tuple[int, int]] = set()

    def add(kind: str, a: int, b: int, section: str) -> None:
        if b <= a or (a, b) in seen:
            return
        seen.add((a, b))
        out.append(Unit(kind, a, b, _anchor(_line_of(starts, a), section, pages)))

    if fmt == "json" and text.strip():
        add("data", 0, len(text.rstrip()), "")
    marks = [(starts[n - 1], starts[n] if n < len(starts) else len(text)) for n, _ in pages]
    for s in segs:
        add(s.kind, s.start, s.end, s.section)
        inside = [(a, b) for a, b in marks if s.start <= a < s.end]
        if inside:  # a block across a page marker is also its pieces, each under its page
            at = s.start
            for a, b in inside:
                piece = text[at:a].strip()
                if piece:
                    p0 = text.index(piece, at)
                    add(s.kind, p0, p0 + len(piece), s.section)
                at = b
            piece = text[at : s.end].strip()
            if piece:
                p0 = text.index(piece, at)
                add(s.kind, p0, p0 + len(piece), s.section)
        if fmt in LINE_FORMATS:
            a, b = _widen(text, s.start, s.end)
            add("line", a, b, s.section)
        if cells and s.kind in ("row", "header"):
            for _, cs, ce in s.cells:
                add("cell", cs, ce, s.section)
    return out


def sections_of(segs: Sequence[Segment], fmt: str) -> list[tuple[int, int]]:
    """The record's sections as segment ranges (ADR-P07 G4); one for a table."""
    if fmt in ("csv", "json") or not segs:
        return [(0, len(segs))]
    return record_sections(segs)


def _records(ctx: ToolContext, reading: InputReading) -> tuple[str, ...]:
    if ctx.reader is None:
        return ()
    return tuple(sorted(ctx.reader.records(reading.source, reading.collection)))


def _query(ctx: ToolContext, tool: str, args: Mapping[str, Any], hits: Sequence[str]) -> InputRef:
    """The recorded query of ``tool`` with ``args``: its version hashes the hits as a set."""
    qid = f"{ctx.agent}.{tool}?" + json.dumps(dict(args), sort_keys=True, separators=(",", ":"))
    hashed = hashlib.sha256("\n".join(sorted(set(hits))).encode("utf-8")).hexdigest()
    return InputRef("query", qid, "q-" + hashed[:16])


def resolve(ctx: ToolContext, reading: InputReading, ref: str) -> str | Err:
    """The input record ``ref`` names: its record ID, or a path inside the collection."""
    ref = ref.strip()
    if ref.lower().startswith(("http://", "https://")):
        return Err("not_an_input", f"{ref!r} is a URL: fetch_url reads a web page", paths=("/ref",))
    if not ref or ref == reading.collection or ".." in ref.split("/"):
        return Err("not_an_input", f"{ref!r} is not an input", paths=("/ref",))
    records = _records(ctx, reading)
    if ref in records:
        return ref
    tail = [r for r in records if r.endswith("/" + ref.strip("/"))]
    if len(tail) == 1:
        return tail[0]
    if len(tail) > 1:
        return Err(
            "ambiguous_ref",
            f"{ref!r} names {len(tail)} inputs: {tail[:5]}; give the full ref list_inputs shows",
            paths=("/ref",),
        )
    return Err(
        "not_found",
        f"{ref!r} is not among the inputs; list_inputs lists them",
        paths=("/ref",),
        item=ref,
    )


# --- list_inputs -----------------------------------------------------------------------


def _decide(
    ctx: ToolContext, reading: InputReading, found: ReturnedText, fmt: str, segs: Sequence[Segment]
) -> dict[str, Any]:
    """The record's kind and, when it has several sections, each section's kind."""
    assert reading.decide is not None
    state = document_preview(found.record_id, fmt, found.text, segs)
    kind, decided, _ = decide_kind(ctx.models, reading.decide, reading.kinds, state)
    row: dict[str, Any] = {"kind": kind, "decided": decided}
    parts = sections_of(segs, fmt)
    if len(parts) > 1:
        listed = []
        for n, (a, b) in enumerate(parts):
            state = document_preview(f"{found.record_id}#section-{n}", fmt, found.text, segs[a:b])
            k, d, _ = decide_kind(ctx.models, reading.decide, reading.kinds, state)
            listed.append(
                {"section": n, "heading": section_heading(segs, (a, b)), "kind": k, "decided": d}
            )
        row["sections"] = listed
    return row


def list_inputs(ctx: ToolContext, args: Mapping[str, Any], reading: InputReading) -> ToolOutcome:
    """The inputs, by ref (module doc); filtered by kind and format, then paged."""
    kinds = {str(k) for k in args.get("kinds", ())}
    if kinds and reading.decide is None:
        return ToolOutcome(
            Err("no_kinds", "this agent declares no document kinds to filter by", paths=("/kinds",))
        )
    formats = {str(f) for f in args.get("formats", ())}
    offset = int(args.get("offset", 0))
    limit = int(args.get("limit", LIST_LIMIT))
    rows: list[dict[str, Any]] = []
    for r in _records(ctx, reading):
        found = ctx.reader.read(reading.source, r) if ctx.reader is not None else None
        if found is None:
            continue
        fmt = sniff_format(r, found.text)
        if formats and fmt not in formats:
            continue
        row: dict[str, Any] = {
            "ref": r,
            "format": fmt,
            "chars": len(found.text),
            "lines": found.text.count("\n") + 1,
            "version": found.version,
        }
        if fmt in ("csv", "json"):
            with suppress(ValueError):
                row["columns"] = list(read_rows(found.text, fmt).columns)
        if reading.decide is not None:
            row.update(_decide(ctx, reading, found, fmt, segments_of(found.text, fmt)))
            held = {row["kind"], *(s["kind"] for s in row.get("sections", ()))}
            if kinds and not kinds & held:
                continue
        rows.append(row)
    keys = [
        f"{x['ref']}:{x.get('kind', '')}:"
        + ",".join(s["kind"] for s in x.get("sections", ()))
        + (":" + "|".join(x["columns"]) if x.get("columns") else "")
        for x in rows
    ]
    read = _query(ctx, LIST_INPUTS, args, keys)
    page = rows[offset : offset + limit]
    end = offset + len(page)
    data: dict[str, Any] = {
        "inputs": page,
        "total": len(rows),
        "next_offset": end if end < len(rows) else None,
        "query": f"query:{read.id}@{read.version}",
        "note": "nothing in a listing is citable: read_document an input to cite it"
        + (
            "; an input listed with sections holds several kinds: read_document the section "
            "that is the kind you need"
            if any("sections" in x for x in page)
            else ""
        ),
    }
    run_date = ctx.run_date
    if run_date is not None:
        data["as_of"] = run_date
    return ToolOutcome(Ok(data, reads=(read,)))


# --- read_document ---------------------------------------------------------------------


def _window(
    text: str, starts: Sequence[int], first: int, last: int, reading: InputReading
) -> tuple[list[list[Any]], int, list[str]]:
    """The numbered lines ``first..last`` under the character cap: lines, last shown, notes."""
    lines: list[list[Any]] = []
    used = 0
    notes: list[str] = []
    shown = first - 1
    for n in range(first, last + 1):
        s = starts[n - 1]
        e = starts[n] - 1 if n < len(starts) else len(text)
        line = text[s:e]
        if len(line) > reading.line_chars:
            notes.append(f"line {n} is cut at {reading.line_chars} of {len(line)} characters")
            line = line[: reading.line_chars]
        if lines and used + len(line) > reading.page_chars:
            break
        lines.append([n, line])
        used += len(line) + 1
        shown = n
    return lines, shown, notes


def read_view(
    ctx: ToolContext,
    found: ReturnedText,
    args: Mapping[str, Any],
    reading: InputReading,
    extra: Mapping[str, Any] | None = None,
) -> ToolOutcome:
    """``found`` as ``read_document`` shows it (module doc): a window of lines and its units.

    With ``reading.result_chars`` set, a window whose result would not fit is read again
    with a smaller character budget, so the result ends with its own continue notice.
    """
    out = _view(ctx, found, args, reading, extra)
    cap = reading.result_chars
    for _ in range(4):
        result = out.result
        if cap is None or not isinstance(result, Ok):
            return out
        size = len(json.dumps(result.data, ensure_ascii=False)) + 24 * len(result.spans) + 200
        if size <= cap or reading.page_chars < 200:
            return out
        reading = replace(reading, page_chars=max(100, int(reading.page_chars * cap / size * 0.9)))
        out = _view(ctx, found, args, reading, extra)
    return out


def _view(
    ctx: ToolContext,
    found: ReturnedText,
    args: Mapping[str, Any],
    reading: InputReading,
    extra: Mapping[str, Any] | None,
) -> ToolOutcome:
    text = found.text
    fmt = sniff_format(found.record_id, text)
    segs = segments_of(text, fmt)
    starts = _line_starts(text)
    total = len(starts)
    if args.get("where") is not None:
        return _where_view(found, fmt, segs, starts, args, reading, extra)
    lo, hi = 1, total
    section = args.get("section")
    parts = sections_of(segs, fmt)
    within = segs
    if section is not None:
        n = int(section)
        if not 0 <= n < len(parts) or len(parts) < 2:
            return ToolOutcome(
                Err(
                    "no_section",
                    f"{found.record_id} has no section {n}"
                    + (f"; it has {len(parts)}" if len(parts) > 1 else ""),
                    paths=("/section",),
                    item=found.record_id,
                )
            )
        a, b = parts[n]
        within = segs[a:b]
        lo = _line_of(starts, within[0].start)
        hi = _line_of(starts, max(within[-1].end - 1, within[-1].start))
    offset = int(args.get("offset", lo))
    if offset > hi:
        return ToolOutcome(
            Err(
                "offset_past_end",
                f"offset {offset} is past the end: {found.record_id} has lines {lo}-{hi}",
                paths=("/offset",),
                item=found.record_id,
            )
        )
    first = max(offset, lo)
    limit = int(args.get("limit", reading.page_lines))
    lines, shown, notes = _window(text, starts, first, min(hi, first + limit - 1), reading)
    ws = starts[first - 1]
    we = starts[shown] - 1 if shown < total else len(text)
    every = units_of(text, fmt, within, cells=bool(args.get("cells", False)))
    units = [u for u in every if u.start < max(we, ws + 1) and u.end > ws]
    data: dict[str, Any] = {
        **(extra or {}),
        "ref": found.record_id,
        "version": found.version,
        "format": fmt,
        "total_lines": total,
    }
    if section is not None:
        data["section"] = int(section)
    marks: list[tuple[int, str]] = [(line, label) for line, label in _pages(text, starts)]
    paged = {line for line, _ in marks}  # a page marker the segmenter also reads as a heading
    heads = ((_line_of(starts, s.start), s.text[:80]) for s in segs if s.kind == "heading")
    marks += [(line, label) for line, label in heads if line not in paged]
    if marks:
        data["anchors"] = [[label, line] for line, label in sorted(marks)[:60]]
    if bool(args.get("extract", False)):
        refused = _extract(ctx, reading, found, fmt, within, units, data)
        if refused is not None:
            return ToolOutcome(refused)
    data["lines"] = lines
    data["first_line"], data["last_line"] = first, shown
    data["start"] = ws
    if shown < hi:
        data["next_offset"] = shown + 1
        notes.append(
            f"showing lines {first}-{shown} of {hi}; call read_document with "
            f"offset={shown + 1} to continue"
        )
    if notes:
        data["truncated"] = "; ".join(notes)
    data["units"] = [_unit_json(n, u, starts, text) for n, u in enumerate(units)]
    data["cite"] = (
        f"{CITE_UNIT}; a unit is [n, kind, first line, last line, start, end, anchor]; start "
        "and end count characters from the start of the document, and 'start' "
        "is where the first line shown begins"
    )
    drafts = tuple(
        SpanDraft(found.source, found.record_id, found.version, u.start, u.end) for u in units
    )
    read = InputRef("record", found.record_id, found.version)
    return ToolOutcome(Ok(data, spans=drafts, reads=(read,)))


def _where_view(
    found: ReturnedText,
    fmt: str,
    segs: Sequence[Segment],
    starts: Sequence[int],
    args: Mapping[str, Any],
    reading: InputReading,
    extra: Mapping[str, Any] | None,
) -> ToolOutcome:
    """The header and the rows of a CSV table whose ``column`` holds one of ``values``.

    The read is the record part those rows are (``where_part``), at that part's version, so
    a change to another row moves nothing this read fed (ADR-P05 D6). The rows shown are
    held to the window's caps like any window (``line_chars``, ``page_chars``); rows past
    the cap are not shown and the result says to narrow ``values``. The read stays the
    whole part, so a change to a row not shown still reopens what this read fed.
    """
    where = args["where"]
    column, values = str(where["column"]), [str(v) for v in where["values"]]
    text = found.text
    try:
        table = read_rows(text, "csv") if fmt == "csv" else None
    except ValueError:
        table = None
    if table is None or table.delimiter not in ("", ","):
        return ToolOutcome(
            Err(
                "where_not_a_table",
                f"{found.record_id} is no comma-separated table: read it whole",
                paths=("/where",),
            )
        )
    fragment = where_part(found.record_id, column, values).partition("#")[2]
    if column not in table.columns or part_text(text, fragment) is None:
        return ToolOutcome(
            Err(
                "no_column",
                f"{found.record_id} has no column {column!r}; its "
                f"columns are {list(table.columns)}",
                paths=("/where/column",),
            )
        )
    kept = [r for r in table.rows if r.value(column) in values]
    spans = [(s.start, s.end) for s in segs if s.kind == "header"]
    spans += [(r.start, r.end) for r in kept]
    chosen = [s for s in segs if any(a <= s.start < max(b, a + 1) for a, b in spans)]
    numbers = sorted(
        {
            n
            for a, b in spans
            for n in range(_line_of(starts, a), _line_of(starts, max(b - 1, a)) + 1)
        }
    )
    lines: list[list[Any]] = []
    notes: list[str] = []
    used = 0
    shown_end = 0
    for n in numbers:
        s = starts[n - 1]
        e = starts[n] - 1 if n < len(starts) else len(text)
        line = text[s:e]
        if len(line) > reading.line_chars:
            notes.append(f"line {n} is cut at {reading.line_chars} of {len(line)} characters")
            line = line[: reading.line_chars]
        if lines and used + len(line) > reading.page_chars:
            notes.append(
                f"showing {len(lines)} of {len(numbers)} lines; narrow values to read the rest"
            )
            break
        lines.append([n, line])
        used += len(line) + 1
        shown_end = e
    chosen = [c for c in chosen if c.start < max(shown_end, 1)]
    shown_rows = sum(1 for r in kept if r.start < max(shown_end, 1))
    units = units_of(text, fmt, chosen, cells=bool(args.get("cells", False)))
    data: dict[str, Any] = {
        **(extra or {}),
        "ref": found.record_id,
        "version": found.version,
        "format": fmt,
        "total_lines": len(starts),
        "where": {"column": column, "values": values},
        "rows": shown_rows,
        "matched_rows": len(kept),
        "lines": lines,
        "start": starts[numbers[0] - 1] if numbers else 0,
        "units": [_unit_json(n, u, starts, text) for n, u in enumerate(units)],
        "cite": CITE_UNIT,
    }
    if notes:
        data["truncated"] = "; ".join(notes)
    drafts = tuple(
        SpanDraft(found.source, found.record_id, found.version, u.start, u.end) for u in units
    )
    part = where_part(found.record_id, column, values)
    version = part_version(text, fragment, found.version) or found.version
    return ToolOutcome(Ok(data, spans=drafts, reads=(InputRef("record", part, version),)))


def _unit_json(n: int, u: Unit, starts: Sequence[int], text: str) -> list[Any]:
    a, b = _line_of(starts, u.start), _line_of(starts, max(u.end - 1, u.start))
    kind = u.kind if u.field is None else f"field:{u.field}"
    row: list[Any] = [n, kind, a, b, u.start, u.end, u.anchor]
    if u.field is not None:
        row.append(text[u.start : u.end])
    return row


def _extract(
    ctx: ToolContext,
    reading: InputReading,
    found: ReturnedText,
    fmt: str,
    within: Sequence[Segment],
    units: list[Unit],
    data: dict[str, Any],
) -> Err | None:
    """Decide-then-extract over ``within``; each value's ranges become units (module doc)."""
    if reading.decide is None:
        return Err(
            "no_extraction", "this agent declares no document kinds to extract", paths=("/extract",)
        )
    shown = [
        SpanDraft(found.source, found.record_id, found.version, s.start, s.end)
        for s in within
        if s.end > s.start
    ]
    state = document_preview(found.record_id, fmt, found.text, within)
    got = read_document(ctx.models, reading.decide, reading.kinds, found, shown, state)
    data.update(got.as_json())
    index = {(u.start, u.end): n for n, u in enumerate(units)}
    starts = _line_starts(found.text)
    pages = _pages(found.text, starts)
    fields: dict[str, Any] = {}
    for name, value in got.values.items():
        cited: list[int] = []
        for a, b in got.ranges.get(name, ()):
            if (a, b) not in index:
                section = next((s.section for s in within if s.start <= a < s.end), "")
                index[(a, b)] = len(units)
                units.append(
                    Unit("field", a, b, _anchor(_line_of(starts, a), section, pages), name)
                )
            cited.append(index[(a, b)])
        shown_value = list(value) if isinstance(value, tuple) else value
        fields[name] = {"value": shown_value, "units": cited}
    data["fields"] = fields
    return None


def read_input(ctx: ToolContext, args: Mapping[str, Any], reading: InputReading) -> ToolOutcome:
    """``read_document``'s body: resolve the ref, read the record, show :func:`read_view`."""
    ref = resolve(ctx, reading, str(args["ref"]))
    if isinstance(ref, Err):
        return ToolOutcome(ref)
    found = ctx.reader.read(reading.source, ref) if ctx.reader is not None else None
    if found is None:
        return ToolOutcome(Err("fetch_failed", f"{ref} could not be read", item=ref))
    return read_view(ctx, found, args, reading)


# --- search_inputs ---------------------------------------------------------------------


def _terms(query: str) -> list[str]:
    return list(dict.fromkeys(w.casefold() for w in _WORD.findall(query) if len(w) >= 2))


def _shown_text(text: str, fmt: str, u: Unit) -> str:
    part = text[u.start : u.end]
    return visible(part) if fmt == "html" else " ".join(part.split())


def search_inputs(ctx: ToolContext, args: Mapping[str, Any], reading: InputReading) -> ToolOutcome:
    """Ranked passages matching ``query`` or ``terms`` (module doc).

    ``query`` is words: any of them (the default), all of them, or the whole phrase.
    ``terms`` is a list of words or phrases, each matched whole: any of them, or all.
    """
    match = str(args.get("match", "any"))
    if args.get("terms"):
        terms = list(dict.fromkeys(" ".join(str(t).split()).casefold() for t in args["terms"]))
        query = " | ".join(terms)
    else:
        query = " ".join(str(args.get("query", "")).split())
        terms = [query.casefold()] if match == "phrase" else _terms(query)
    terms = [t for t in terms if t]
    if not terms:
        return ToolOutcome(
            Err("query_empty", "give a query or terms with a word to search for", paths=("/query",))
        )
    formats = {str(f) for f in args.get("formats", ())}
    limit = int(args.get("limit", SEARCH_LIMIT))
    if args.get("ref") is not None:
        one = resolve(ctx, reading, str(args["ref"]))
        if isinstance(one, Err):
            return ToolOutcome(one)
        records: tuple[str, ...] = (one,)
    else:
        records = _records(ctx, reading)
    need = len(terms) if match == "all" else 1
    hits: list[tuple[int, str, int, int, dict[str, Any]]] = []
    searched = 0
    for r in records:
        found = ctx.reader.read(reading.source, r) if ctx.reader is not None else None
        if found is None:
            continue
        fmt = sniff_format(r, found.text)
        if formats and fmt not in formats:
            continue
        searched += 1
        starts = _line_starts(found.text)
        units = units_of(found.text, fmt, segments_of(found.text, fmt))
        # A JSON value's whole unit is searched only when none of its rows matched.
        mine: list[dict[str, Any]] = []
        for u in sorted(units, key=lambda x: x.kind == "data"):
            if u.kind == "line" or (u.kind == "data" and mine):
                continue
            shown = _shown_text(found.text, fmt, u)
            low = shown.casefold()
            present = [t for t in terms if t in low]
            if len(present) < need:
                continue
            count = sum(low.count(t) for t in present)
            at = min(low.find(t) for t in present)
            a = max(0, at - SNIPPET_CHARS // 4)
            line = _line_of(starts, u.start)
            mine.append(
                {
                    "ref": r,
                    "anchor": u.anchor,
                    "kind": u.kind,
                    "lines": [line, _line_of(starts, max(u.end - 1, u.start))],
                    "snippet": shown[a : a + SNIPPET_CHARS],
                    "score": len(present) * 100 + count,
                }
            )
        for h in mine:
            hits.append((-int(h["score"]), r, int(h["lines"][0]), len(hits), h))
    hits.sort(key=lambda h: h[:4])
    ranked = [h[-1] for h in hits]
    read = _query(ctx, SEARCH_INPUTS, args, [str(h["ref"]) for h in ranked])
    data: dict[str, Any] = {
        "query": query,
        "match": match,
        "inputs_searched": searched,
        "inputs_hit": sorted({str(h["ref"]) for h in ranked}),
        "total_hits": len(ranked),
        "hits": ranked[:limit],
        "recorded": f"query:{read.id}@{read.version}",
        "note": "a hit is not citable: read_document its ref (offset at its first line) to cite it",
    }
    if len(ranked) > limit:
        data["truncated"] = (
            f"showing {limit} of {len(ranked)} hits; narrow the query or raise limit"
        )
    return ToolOutcome(Ok(data, reads=(read,)))


# --- declarations ----------------------------------------------------------------------

_FORMATS = {"type": "array", "items": {"enum": list(FORMATS)}}
_REF = {"type": "string", "minLength": 1, "maxLength": 512}


def list_schema(reading: InputReading) -> dict[str, Any]:
    """``list_inputs``'s arguments; ``kinds`` only for an agent that declares kinds."""
    props: dict[str, Any] = {
        "formats": _FORMATS,
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    }
    if reading.decide is not None:
        props["kinds"] = {"type": "array", "items": {"enum": list(reading.kind_names())}}
    return {"type": "object", "properties": props, "additionalProperties": False}


def read_schema(reading: InputReading) -> dict[str, Any]:
    """``read_document``'s arguments; ``extract`` only for an agent that declares kinds."""
    props: dict[str, Any] = {
        "ref": _REF,
        "offset": {"type": "integer", "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LINES},
        "section": {"type": "integer", "minimum": 0},
        "cells": {"type": "boolean"},
        "where": {
            "type": "object",
            "properties": {
                "column": {"type": "string", "minLength": 1, "maxLength": 200},
                "values": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {"type": "string", "maxLength": 200},
                },
            },
            "required": ["column", "values"],
            "additionalProperties": False,
        },
    }
    if reading.decide is not None:
        props["extract"] = {"type": "boolean"}
    return {
        "type": "object",
        "properties": props,
        "required": ["ref"],
        "additionalProperties": False,
    }


SEARCH_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 2, "maxLength": 400},
        "terms": {
            "type": "array",
            "minItems": 1,
            "maxItems": 40,
            "items": {"type": "string", "minLength": 2, "maxLength": 200},
        },
        "ref": _REF,
        "formats": _FORMATS,
        "match": {"enum": ["any", "all", "phrase"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "anyOf": [{"required": ["query"]}, {"required": ["terms"]}],
    "additionalProperties": False,
}


def input_tools(agent: str, reading: InputReading) -> tuple[ToolDecl, ...]:
    """``list_inputs``, ``read_document`` and ``search_inputs`` for ``agent`` (module doc)."""
    collections = frozenset({reading.collection})
    aliases = reading.aliases()
    kinds = ""
    extract = ""
    if reading.decide is not None:
        kinds = (
            " Each input's kind (" + ", ".join(reading.kind_names()) + ") is decided "
            "from its structure; one with sections lists each section's kind."
        )
        extract = (
            " extract=true also decides its kind and fills that kind's typed fields, "
            "each with the units that state it; 'unresolved' names what you must read "
            "yourself."
        )

    def listing(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        return list_inputs(ctx, args, reading)

    def reader(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        return read_input(ctx, args, reading)

    def searcher(ctx: ToolContext, args: Mapping[str, Any]) -> ToolOutcome:
        return search_inputs(ctx, args, reading)

    listing.__name__, reader.__name__, searcher.__name__ = INPUT_TOOLS
    return (
        ToolDecl(
            ToolSpec(
                LIST_INPUTS,
                agent,
                "read",
                "List the inputs the person gave you: each ref with its format, size in "
                "characters and lines, and version." + kinds + " 'as_of' is today's date as "
                "the run counts it. Nothing in the listing is citable.",
                list_schema(reading),
                collections=collections,
                recorded_query=True,
                result_key="/ref",
                retry=reading.retry,
                model_aliases=aliases,
            ),
            listing,
        ),
        ToolDecl(
            ToolSpec(
                READ_DOCUMENT,
                agent,
                "read",
                "Read one input by ref: numbered lines (offset from 1, limit lines; a cut "
                "window says the offset to continue at) and its units, the citable passages "
                "(headings, paragraphs, items, rows), each with its anchor. Cite unit n by "
                "the n-th citation span. section=N reads one section the listing names; "
                "cells=true makes each table cell a unit, to cite a short value." + extract,
                read_schema(reading),
                collections=collections,
                mints_citation_spans=True,
                retry=reading.retry,
                model_aliases=aliases,
            ),
            reader,
        ),
        ToolDecl(
            ToolSpec(
                SEARCH_INPUTS,
                agent,
                "read",
                "Search the inputs (or one, by ref) for words: ranked passages with their "
                "ref, anchor, lines and a snippet. A query matches any of its words (the "
                "default), all of them, or the exact phrase; terms is a list of words or "
                "phrases, each matched whole. A hit is not citable: read_document it to cite it.",
                SEARCH_SCHEMA,
                collections=collections,
                recorded_query=True,
                result_key="/ref",
                retry=reading.retry,
            ),
            searcher,
        ),
    )


__all__ = ["INPUT_TOOLS", "InputReading", "Unit", "input_tools", "read_view", "units_of"]
