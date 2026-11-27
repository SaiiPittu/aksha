"""How the research tools understand a record: decide its kind, pick its schema, extract.

A tool never matches one venture's layout. It cuts the record into passages by structure
(``segment``, ``read_rows``: blocks, rows, cells, each an exact range of the raw text) and
runs the runtime's document pipeline (``read_document``, ADR-P07 G3) through the agent's
fast-class aliases:

1. **Decide** (``doc_kind``, classification): which of :data:`KINDS` the record is, or
   ``other``, from a preview code renders from structure (``document_preview``).
2. **Pick the schema**: each kind names the extraction alias whose fields are its schema.
3. **Extract**: the kind's alias fills its schema; every value comes back with a span.

=====================  ====================  ===============================================
Kind                   Schema alias          What it extracts
=====================  ====================  ===============================================
``competitor_page``    ``page_facts``        who makes or sells it (``company``,
                                             ``product``), when it was published, and quoted
                                             spans: each price, each sentence saying two
                                             names are one company, each marketing claim,
                                             text addressed to an automated reader
``pricing_table``      ``price_table``       per row: the seller, the product or plan, the
                                             stated price (``sellers``, ``products``,
                                             ``prices``, one quote per row)
``customer_reviews``   ``feedback_items``    each customer voice of a text; a table's
                                             columns are named by ``table_roles``
``market_report``      ``report_facts``      the companies it names, its figures, its
                                             claims, when it was published
``conversation``       ``conversation_facts`` each price a message quotes, and when it was
                                             sent (``prices``, ``published``)
``internal_note``      none                  read by the Task's general model
=====================  ====================  ===============================================

A record whose kind is ``other`` or undecided, and every field the extractor abstained
on, is left to the System 2 model: the tool returns the record's passages, one citation
span each, and says what is unresolved, so the general model reads and cites it itself.
``theme_match`` (classification) says which of the request's themes one voice speaks to.

Every quoted answer comes back as a span inside one passage; the tool shows it as its own
passage (a ``price``, a ``relationship``) and the runtime mints it, so a value is citable
to the words that state it. Code still applies the rules that are not readings: a price's
currency, amount and basis are read from its quote (``parsing.price_fields``); a block
under ``display:none`` is hidden.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from aksha.runtime.api import (
    OTHER_KIND,
    DocumentKind,
    DocumentReading,
    QuestionSpec,
    ReturnedText,
    Segment,
    SpanDraft,
    ToolModels,
    decide_kind,
    document_preview,
    read_document,
    read_rows,
    record_sections,
    section_heading,
    segment,
    sniff_format,
    visible,
)

from . import parsing
from .parsing import Page, Passage, Table

DECIDE = "doc_kind"
"""The decide alias: which kind of record this is."""

KINDS = (
    DocumentKind(
        "competitor_page",
        "a seller's own page, listing, brochure or datasheet for a product or service it "
        "offers, often with its prices and specifications",
        "page_facts",
    ),
    DocumentKind(
        "pricing_table",
        "a table, spreadsheet or price list naming several sellers or products, each with "
        "its price or plan",
        "price_table",
    ),
    DocumentKind(
        "customer_reviews",
        "customers' or users' own words: reviews, ratings, survey answers, interview "
        "notes, support tickets",
        "feedback_items",
    ),
    DocumentKind(
        "market_report",
        "a third party's report, article or analysis about a market that names several companies",
        "report_facts",
    ),
    DocumentKind(
        "conversation",
        "a chat export, a call log or a message thread between people, each message with "
        "its sender and time, such as a seller's representative quoting a price",
        "conversation_facts",
    ),
    DocumentKind(
        "internal_note",
        "the person's own notes, plans, priorities, minutes or emails",
    ),
)
"""The kinds of record the research agent reads; extend it with a kind and its alias."""

KIND_NAMES = (*(k.name for k in KINDS), OTHER_KIND)
SELLER_KINDS = ("competitor_page", "pricing_table")
VOICE_KINDS = ("customer_reviews",)

HERO_CLASS = re.compile(r"\b(hero|tagline|banner|headline|lead|strapline)\b", re.I)
"""Classes a page's own marketing line is set in: presentational markup, not a layout."""


def record_format(record: str, text: str) -> str:
    """The record's format, from its content first (``sniff_format``)."""
    return sniff_format(record, text)


def drafts(found: ReturnedText, segs: Sequence[Segment], source: str) -> list[SpanDraft]:
    """One draft per segment, in order: the passages an alias is shown."""
    return [SpanDraft(source, found.record_id, found.version, s.start, s.end) for s in segs]


def _segments(found: ReturnedText, fmt: str) -> list[Segment]:
    try:
        return list(segment(found.text, fmt))
    except ValueError:
        return list(segment(found.text, "text"))


def decide(models: ToolModels | None, found: ReturnedText, fmt: str) -> tuple[str, bool]:
    """``(kind, decided)``: step 1 alone, for a listing; ``other`` when it abstains."""
    state = document_preview(found.record_id, fmt, found.text, _segments(found, fmt))
    kind, decided, _ = decide_kind(models, DECIDE, KINDS, state)
    return kind, decided


def sections(found: ReturnedText, fmt: str) -> list[list[Segment]]:
    """The record's sections (``record_sections``): one for a table or an unsectioned text."""
    segs = _segments(found, fmt)
    if fmt in ("csv", "json"):
        return [segs]
    return [segs[a:b] for a, b in record_sections(segs)]


def section_of(found: ReturnedText, fmt: str, n: int | None) -> list[Segment] | None:
    """Section ``n``'s segments; every segment when ``n`` is None; None past the last."""
    if n is None:
        return _segments(found, fmt)
    got = sections(found, fmt)
    return got[n] if 0 <= n < len(got) else None


def decide_sections(
    models: ToolModels | None, found: ReturnedText, fmt: str
) -> list[dict[str, Any]]:
    """Each section's kind, decided on its own; empty when the record is one section.

    A record holding several kinds (a memo with a seller section and a section of customer
    quotes) lists each, so a phase reads the section that is its kind (ADR-P07 G4).
    """
    got = sections(found, fmt)
    if len(got) < 2:
        return []
    out = []
    for n, segs in enumerate(got):
        state = document_preview(f"{found.record_id}#section-{n}", fmt, found.text, segs)
        kind, decided, _ = decide_kind(models, DECIDE, KINDS, state)
        out.append({"section": n, "heading": section_heading(segs, (0, len(segs))),
                    "kind": kind, "decided": decided})  # fmt: skip
    return out


def _overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _url(segs: Sequence[Segment]) -> str | None:
    for s in segs:
        if s.kind != "meta":
            continue
        a = s.attrs
        if "canonical" in a.get("rel", "") and a.get("href"):
            return a["href"]
        if a.get("property") in ("og:url",) and a.get("content"):
            return a["content"]
        if a.get("key") in ("url", "link", "website") and a.get("value"):
            return a["value"]
    for s in segs:
        if s.kind == "data":
            m = re.search(r'"url"\s*:\s*"([^"]+)"', s.text)
            if m:
                return m.group(1)
    return None


def _title(raw: str, segs: Sequence[Segment]) -> str:
    for s in segs:
        if s.kind == "meta" and s.attrs.get("key") == "title":
            return s.attrs.get("value", "")
    for s in segs:
        if s.kind == "title":
            return s.text
    for s in segs:
        if s.kind == "heading":
            return s.text
    return ""


def _first(reading: DocumentReading, name: str) -> tuple[str | None, tuple[int, int] | None]:
    value, at = reading.first(name)
    return (None if value is None else str(value)), at


def read_page(
    models: ToolModels | None,
    found: ReturnedText,
    fmt: str,
    source: str,
    segs: Sequence[Segment] | None = None,
) -> tuple[Page, DocumentReading]:
    """Any record as a page (module doc): decide, extract by its kind, then its passages.

    The page's passages are every structural block in document order; a quoted value the
    extractor returned is its own passage (``price``, ``relationship``, ``claim``). A row
    of a pricing table, or any block, carries the seller and product quoted in it. ``segs``
    reads one section only (``section_of``); its ranges are still the record's.
    """
    raw = found.text
    segs = list(segs) if segs is not None else _segments(found, fmt)
    shown = drafts(found, segs, source)
    state = document_preview(found.record_id, fmt, raw, segs)
    reading = read_document(models, DECIDE, KINDS, found, shown, state)
    passages: list[Passage] = []
    published, at = _first(reading, "published")
    if at is not None:
        passages.append(Passage("published", at[0], at[1], raw[at[0] : at[1]]))
    title = _title(raw, segs)
    for s in segs:
        if s.kind == "title":
            passages.append(Passage("title", s.start, s.end, raw[s.start : s.end]))
            break
    names: dict[str, str] = {}
    for field, kind in (("product", "product"), ("company", "brand")):
        value, at = _first(reading, field)
        if value is not None and at is not None:
            names[kind] = raw[at[0] : at[1]]
            passages.append(Passage(kind, at[0], at[1], raw[at[0] : at[1]],
                                    fields={"name": raw[at[0] : at[1]]}))  # fmt: skip
    got = reading.ranges
    prices = got.get("prices", ())
    relations = got.get("relationships", ())
    claims = got.get("claims", ())
    addressed = got.get("addressed_to_agents", ())
    sellers = got.get("sellers", ()) + got.get("companies", ())
    products = got.get("products", ())
    header: list[str] = []
    if fmt in ("csv", "json"):
        try:
            header = list(read_rows(raw, fmt).columns)
        except ValueError:
            header = []
    above: tuple[int, str] | None = None  # the last row's seller: its column and its name
    for s in segs:
        r = (s.start, s.end)
        if s.kind == "meta" and fmt == "json" and s.attrs.get("value_start"):
            # A feed's own fields outside its records (its tagline, its notes) are what
            # the record says: each is a passage over its value.
            vs, ve = int(s.attrs["value_start"]), int(s.attrs["value_end"])
            hero = HERO_CLASS.search(s.attrs.get("key", "").replace("_", " "))
            claimed = any(_overlaps((vs, ve), a) for a in claims)
            passages.append(Passage("claim" if hero or claimed else "text", vs, ve, raw[vs:ve],
                                    s.section, instruction_like=any(
                                        _overlaps((vs, ve), a) for a in addressed)))  # fmt: skip
            continue
        if s.kind in ("meta", "data", "title", "footer"):
            continue
        if s.kind == "heading":
            if fmt not in ("csv", "json"):
                header = []
            passages.append(Passage("heading", s.start, s.end, raw[s.start : s.end],
                                    fields={"level": s.level}))  # fmt: skip
            continue
        if s.kind == "header":
            header = [c[0] for c in s.cells]
            continue
        flagged = any(_overlaps(r, a) for a in addressed)
        inside = [p for p in prices if r[0] <= p[0] and p[1] <= r[1]]
        seller = next((raw[x:y] for x, y in sellers if r[0] <= x and y <= r[1]), None)
        product = next((raw[x:y] for x, y in products if r[0] <= x and y <= r[1]), None)
        if s.kind == "row" and s.cells:
            at_cell = next((i for i, c in enumerate(s.cells) for x, y in sellers
                            if r[0] <= x and y <= r[1] and c[1] <= x and y <= c[2]),
                           None)  # fmt: skip
            if seller is not None and at_cell is not None:
                above = (at_cell, seller)
            elif seller is None and above is not None and above[0] < len(s.cells):
                cell = s.cells[above[0]]
                if not cell[0].strip():
                    seller = above[1]  # a blank cell under the seller column: filled down
        who = {k: v for k, v in (("seller", seller), ("product", product)) if v}
        if s.kind == "row" and s.cells:
            values = [c[0] for c in s.cells]
            row = dict(zip(header, values, strict=False)) if header else {}
            label = product or (values[0] if len(values) > 1 else "")
            for kind, quoted in (("seller", sellers), ("offer", products)):
                at = next(((x, y) for x, y in quoted if r[0] <= x and y <= r[1]), None)
                if at is not None:  # a price list names each row's seller: citable on its own
                    passages.append(Passage(kind, at[0], at[1], raw[at[0] : at[1]], s.section,
                                            {"name": raw[at[0] : at[1]], "row": row}))  # fmt: skip
            stated = [(p, parsing.price_fields(visible(raw[p[0] : p[1]]))) for p in inside]
            stated = [(p, f) for p, f in stated if f]
            for (ps, pe), fields in stated:
                passages.append(Passage("price", ps, pe, raw[ps:pe], s.section,
                                        {"row": row, "label": label, **who, **(fields or {})},
                                        instruction_like=flagged))  # fmt: skip
            if stated:
                # A comparison table's other columns (capacity, battery, range) are specs
                # of the row's offer, each labelled with its column, citable alone.
                taken = [p for p, _ in stated] + [q for q in (*sellers, *products)
                                                   if r[0] <= q[0] and q[1] <= r[1]]  # fmt: skip
                for i, (value, cs, ce) in enumerate(s.cells):
                    if ce <= cs or any(_overlaps((cs, ce), t) for t in taken):
                        continue
                    column = header[i] if i < len(header) else ""
                    if not column or value == label:
                        continue
                    passages.append(Passage("spec", cs, ce, raw[cs:ce], s.section,
                                            {"row": row, "label": column, **who},
                                            instruction_like=flagged))  # fmt: skip
                continue
            cell = s.cells[1] if len(s.cells) > 1 else s.cells[0]
            if cell[2] > cell[1]:
                passages.append(Passage("spec", cell[1], cell[2], raw[cell[1] : cell[2]],
                                        s.section, {"row": row, "label": label, **who},
                                        instruction_like=flagged))  # fmt: skip
            continue
        text = raw[s.start : s.end]
        if s.kind == "hidden":
            passages.append(Passage("hidden", s.start, s.end, text, s.section,
                                    instruction_like=True))  # fmt: skip
            continue
        # A relationship sentence that is the whole block names the block's kind; one inside
        # a longer block (a hard-wrapped paragraph of extracted text) is its own passage
        # after the block, so the merge evidence quotes only that sentence. A claim marks
        # its whole block: the block is the vendor's copy.
        narrower: list[Passage] = []
        kind = "text"
        for a in relations:
            if not _overlaps(r, a):
                continue
            if (a[0] - r[0]) + (r[1] - a[1]) <= 2:
                kind = "relationship"
            elif r[0] <= a[0] and a[1] <= r[1]:
                narrower.append(Passage("relationship", a[0], a[1], raw[a[0] : a[1]], s.section))
        if kind == "text" and (
            any(_overlaps(r, a) for a in claims) or HERO_CLASS.search(s.attrs.get("class", ""))
        ):
            kind = "claim"
        passages.append(Passage(kind, s.start, s.end, text, s.section, dict(who),
                                instruction_like=flagged))  # fmt: skip
        passages.extend(narrower)
        listed = reading.kind == "pricing_table"
        for kind_, quoted in (("seller", sellers), ("offer", products)):
            for x, y in quoted if listed else ():  # a price list in prose: each name, alone
                if r[0] <= x and y <= r[1]:
                    passages.append(Passage(kind_, x, y, raw[x:y], s.section,
                                            {"name": raw[x:y], "row": {}}))  # fmt: skip
        for ps, pe in inside:
            fields = parsing.price_fields(visible(raw[ps:pe]))
            if fields is not None:
                passages.append(Passage("price", ps, pe, raw[ps:pe], s.section,
                                        {"row": {}, "label": s.section, **who, **fields}))  # fmt: skip  # noqa: E501
    page = Page(
        published=published,
        title=title,
        product=names.get("product"),
        brand=names.get("brand"),
        url=_url(segs),
        passages=tuple(passages),
    )
    return page, reading


def read_lines(found: ReturnedText, fmt: str) -> tuple[Passage, ...]:
    """Any text record's blocks (headers, headings, paragraphs, items, rows) as lines."""
    raw = found.text
    out = []
    for s in _segments(found, fmt):
        if s.kind == "meta" and fmt != "email":
            continue  # front matter is the file's metadata, not what it says
        out.append(Passage("line", s.start, s.end, raw[s.start : s.end]))
    return tuple(out)


# --- tables -------------------------------------------------------------------------------


def _render_columns(columns: Sequence[str], rows: Sequence[Mapping[str, str]]) -> str:
    lines = ["columns and sample values:"]
    for c in columns:
        sample = [r.get(c, "") for r in rows[:8] if r.get(c, "")]
        lines.append(f"- {c}: " + " | ".join(v[:80] for v in sample))
    return "\n".join(lines)


def column_roles(
    models: ToolModels | None, columns: Sequence[str], rows: Sequence[Mapping[str, str]]
) -> dict[str, str]:
    """Each role's column, by the ``table_roles`` alias.

    A role two columns claim goes to the first; ``other`` goes to none.
    """
    if models is None or not columns:
        return {}
    questions = [
        QuestionSpec(f"c{i}", f"What does the column {c!r} hold?", "choice", parsing.ROLES)
        for i, c in enumerate(columns)
    ]
    judged = models.classify("table_roles", _render_columns(columns, rows), questions)
    out: dict[str, str] = {}
    for j in judged:
        if j.accepted and isinstance(j.value, str) and j.value != "other" and j.value not in out:
            out[j.value] = columns[int(j.name[1:])]
    return out


def _text_column(roles: Mapping[str, str], rows: Sequence[Mapping[str, str]]) -> str | None:
    if "text" in roles:
        return roles["text"]
    lengths: dict[str, int] = {}
    for r in rows:
        for k, v in r.items():
            lengths[k] = lengths.get(k, 0) + len(v)
    return max(lengths, key=lambda k: lengths[k]) if lengths else None


def themes_of(
    models: ToolModels | None, text: str, themes: Sequence[Mapping[str, Any]]
) -> list[str]:
    """The themes ``text`` speaks to, by the ``theme_match`` alias (accepted ``True`` only)."""
    if models is None or not themes:
        return []
    questions = []
    for t in themes:
        terms = [str(x) for x in t.get("terms", ()) or ()]
        hint = f" Terms that often signal it: {', '.join(terms)}." if terms else ""
        prompt = f"Does this customer text speak about {t.get('name') or t.get('id')}?{hint}"
        questions.append(QuestionSpec(f"t:{t['id']}", prompt, "boolean"))
    judged = models.classify("theme_match", text, questions)
    return [j.name[2:] for j in judged if j.accepted and j.value is True]


def read_table(
    models: ToolModels | None,
    found: ReturnedText,
    fmt: str,
    source: str,
    *,
    offset: int = 0,
    limit: int | None = None,
    themes: Sequence[Mapping[str, Any]] = (),
    section: int | None = None,
) -> Table:
    """Rows of a table, or each customer voice of a text, ``limit`` from ``offset``.

    ``total`` counts every row, so a reader can page through an export of any size and
    count over all of it.
    """
    raw = found.text
    if fmt in ("csv", "json"):
        table = read_rows(raw, fmt)
        plain = [{c: v for c, (v, _, _) in r.cells.items()} for r in table.rows]
        roles = column_roles(models, table.columns, plain)
        text_col = _text_column(roles, plain)
        rows = list(table.rows)
        page = rows[offset : None if limit is None else offset + limit]
        passages = []
        for r in page:
            cell = r.cells.get(text_col or "")
            if cell is None or cell[2] <= cell[1]:
                continue
            values = {c: v for c, (v, _, _) in r.cells.items()}
            fields: dict[str, Any] = {
                role: values[col] for role, col in roles.items() if role != "text" and col in values
            }
            fields.setdefault("id", f"row-{r.n + 1}")
            fields["columns"] = {k: v for k, v in values.items() if k != text_col}
            hits = themes_of(models, cell[0], themes)
            if themes:
                fields["themes"] = hits
            passages.append(Passage("review", cell[1], cell[2], raw[cell[1] : cell[2]],
                                    fields=fields))  # fmt: skip
        header = dict(table.header)
        return Table(table.columns, {**roles, "text": text_col or ""}, tuple(passages), header,
                     len(rows))  # fmt: skip
    within = section_of(found, fmt, section)
    if within is None:
        raise ValueError(f"no section {section}")
    # A page's footer, its hidden text and the words a post quotes from another are not
    # anyone's voice here: a quote is counted where it was first said.
    left_out = ("meta", "heading", "title", "data", "footer", "hidden")
    segs = [s for s in within if s.kind not in left_out and s.attrs.get("quoted") != "true"]
    shown = drafts(found, segs, source)
    reading = read_document(models, DECIDE, KINDS, found, shown, "", kind="customer_reviews")
    items = reading.ranges.get("items", ())
    page_items = list(items)[offset : None if limit is None else offset + limit]
    owners: dict[str, int] = {}
    passages = []
    for n, (s, e) in enumerate(page_items, start=offset):
        owner = next((g.attrs.get("id", "") for g in segs if g.start <= s and e <= g.end), "")
        if owner:  # the post an item sits in names it; a second item in one post is its part
            owners[owner] = owners.get(owner, 0) + 1
            owner = owner if owners[owner] == 1 else f"{owner}#{owners[owner]}"
        fields = {"id": owner or f"item-{n + 1}", "columns": {}}
        if themes:
            fields["themes"] = themes_of(models, visible(raw[s:e]), themes)
        passages.append(Passage("review", s, e, raw[s:e], fields=fields))
    return Table((), {"text": "item"}, tuple(passages), {}, len(items))
