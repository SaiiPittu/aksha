"""How the offline research model reads what the shared read tools returned (a test double).

The agent reads with the harness's tools (``list_inputs``, ``read_document``,
``search_inputs``, ``fetch_url``): a document comes back as numbered lines and its units,
each unit one citation span. A live model reads those lines itself. The offline model
(``fakes.py``) reads them by fixed rules instead, and this module is the part that turns
what a tool returned into what those rules read:

1. **The text, as the tool showed it.** The windows of one document (one ``read_document``
   of a ref and section, or one ``fetch_url``) are put back at their ``start`` offsets, so
   a unit's ``start`` and ``end`` are offsets into the text the model holds. A window that
   says it was cut (``next_offset``) is read on (:func:`continuations`) before anything is
   concluded from the document.
2. **A reading of it.** The agent's own offline readers (``fakes_readers.py``, the same
   rules its fast-class aliases are answered by in ``fake`` mode) run over that text in
   process (``OfflineReaders``): what a page states (``reading.read_page``), a note's
   blocks (``reading.read_lines``), an export's rows with the column of the customer's
   words and the themes each row speaks to (``reading.read_table``).
3. **Citations.** Each passage of that reading is cited by the unit with exactly its range,
   else the smallest unit that holds it, quoting the passage's own words.

The views it builds keep the names the rules were written against (``read_note``,
``fetch_page``, ``read_table``, ``search``), and :func:`outgoing` turns the rules' calls
back into the shared tools' calls. Nothing here is agent logic: no module of the agent
imports it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aksha.runtime.api import OfflineReaders, ReturnedText, segment

from . import fakes_readers, reading
from .parsing import Passage

READERS = {
    "page_facts": fakes_readers.PageFacts(),
    "feedback_items": fakes_readers.FeedbackItems(),
    "table_roles": fakes_readers.TableRoles(),
    "theme_match": fakes_readers.ThemeMatch(),
    "doc_kind": fakes_readers.DocKind(),
    "price_table": fakes_readers.PriceTable(),
    "report_facts": fakes_readers.ReportFacts(),
    "conversation_facts": fakes_readers.ConversationFacts(),
}
"""The agent's offline readers by alias, run in process over a text a tool returned."""

VIEW_OF_TASK = {
    "scope": "read_note",
    "discover": "fetch_page",
    "evidence": "fetch_page",
    "voice": "read_table",
}
"""Which view the rules of each Task read a document as."""


def readers() -> OfflineReaders:
    """The offline readers with the agent's declared fields."""
    from .agent import MODEL_ALIASES  # the agent module imports no fake; this is one way

    return OfflineReaders(READERS, MODEL_ALIASES)


def outgoing(name: str, args: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The shared tool call for one call the rules make, by the view's name."""
    a = dict(args)
    section = {} if a.get("section") is None else {"section": int(a["section"])}
    if name == "search":
        out: dict[str, Any] = {"terms": list(a["terms"])}
        if a.get("formats"):
            out["formats"] = list(a["formats"])
        return "search_inputs", out
    if name == "read_note":
        return "read_document", {"ref": a["record"], **section}
    if name == "fetch_page":
        return "read_document", {"ref": a["record"], **section, "extract": True, "cells": True}
    if name == "read_table":
        return "read_document", {"ref": a["record"], **section}
    if name == "fetch_url":
        return "fetch_url", {"extract": True, "cells": True, **a}
    return name, a


@dataclass
class Document:
    """One document as the windows of its reads showed it (module doc, item 1)."""

    name: str
    args: dict[str, Any]
    data: dict[str, Any]
    step: str = ""
    parts: list[tuple[int, str]] = field(default_factory=list)
    units: dict[tuple[int, int], int] = field(default_factory=dict)
    kinds: list[str] = field(default_factory=list)
    cites: list[str] = field(default_factory=list)
    read: set[int] = field(default_factory=set)
    wanted: set[int] = field(default_factory=set)

    def add(self, data: Mapping[str, Any], cites: Sequence[str]) -> None:
        """One more window of this document."""
        lines = data.get("lines", [])
        self.parts.append((int(data.get("start", 0)), "\n".join(str(t) for _, t in lines)))
        self.read.add(int(data.get("first_line", 1)))
        if data.get("next_offset") is not None:
            self.wanted.add(int(data["next_offset"]))
        for i, u in enumerate(data.get("units", ())):
            key = (int(u[4]), int(u[5]))
            if key in self.units or i >= len(cites):
                continue
            self.units[key] = len(self.cites)
            self.kinds.append(str(u[1]))
            self.cites.append(cites[i])
        for k in ("kind", "decided", "decision", "unresolved", "fields"):
            if k in data and k not in self.data:
                self.data[k] = data[k]

    def missing(self) -> list[int]:
        """The offsets a cut window asked to continue at that no window read."""
        return sorted(self.wanted - self.read)

    def text(self) -> str:
        """The document's text: what the windows showed, at their offsets; blanks elsewhere."""
        size = max((s + len(t) for s, t in self.parts), default=0)
        buf = [" "] * size
        for s, t in self.parts:
            buf[s : s + len(t)] = list(t)
        return "".join(buf)

    def returned(self) -> ReturnedText:
        """The text as a returned text of its record, at its version."""
        source = "web" if self.name == "fetch_url" else "inputs"
        return ReturnedText(source, str(self.data["ref"]), str(self.data["version"]), self.text())

    def cite_of(self, start: int, end: int) -> int:
        """The unit that cites ``[start, end)``: exactly it, else the smallest that holds it."""
        exact = self.units.get((start, end))
        if exact is not None:
            return exact
        holding = [(b - a, n) for (a, b), n in self.units.items() if a <= start and end <= b]
        if holding:
            return min(holding)[1]
        overlap = [(abs(a - start), n) for (a, b), n in self.units.items() if a < end and start < b]
        return min(overlap)[1] if overlap else 0


def continuations(docs: Sequence[Document]) -> list[tuple[str, dict[str, Any]]]:
    """The calls that read on where a window was cut (module doc, item 1)."""
    out = []
    for d in docs:
        for offset in d.missing():
            out.append((d.name, {**d.args, "offset": offset}))
    return out


def _numbered(doc: Document, passages: Sequence[Passage]) -> list[dict[str, Any]]:
    return [p.as_json(doc.cite_of(p.start, p.end)) for p in passages]


def _segments(found: ReturnedText, fmt: str) -> list[Any]:
    try:
        return list(segment(found.text, fmt))
    except ValueError:
        return list(segment(found.text, "text"))


def note_view(doc: Document, models: OfflineReaders) -> dict[str, Any]:
    """A note's blocks as numbered lines (``reading.read_lines``)."""
    found = doc.returned()
    fmt = str(doc.data["format"])
    lines = reading.read_lines(found, fmt)
    return {
        "record": found.record_id,
        "version": found.version,
        "format": fmt,
        "lines": [{"n": doc.cite_of(p.start, p.end), "text": p.text} for p in lines],
    }


def page_view(doc: Document, models: OfflineReaders) -> dict[str, Any]:
    """Any document as a page (``reading.read_page``): its identity and its passages."""
    found = doc.returned()
    fmt = str(doc.data["format"])
    segs = _segments(found, fmt)
    page, read = reading.read_page(models, found, fmt, found.source, segs)
    extra: dict[str, Any] = {k: doc.data[k] for k in ("url", "final_url") if k in doc.data}
    section = doc.args.get("section")
    data: dict[str, Any] = {
        **extra,
        "record": found.record_id,
        "version": found.version,
        "format": fmt,
        **({} if section is None else {"section": int(section)}),
        **read.as_json(),
        "published": page.published,
        "title": page.title,
        "product": page.product,
        "brand": page.brand,
        "url": extra.get("url", page.url),
        "passages": _numbered(doc, page.passages),
    }
    if read.needs_reader:
        data["note"] = "the fast readers left " + ", ".join(read.unresolved) + " unresolved"
    return data


def table_view(
    doc: Document, models: OfflineReaders, themes: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """An export's rows, or a text's voices (``reading.read_table``); None when unreadable."""
    found = doc.returned()
    fmt = str(doc.data["format"])
    try:
        table = reading.read_table(models, found, fmt, "inputs", themes=themes)
    except ValueError:
        return None
    rows = [{"n": doc.cite_of(p.start, p.end), **p.fields, "text": p.text} for p in table.passages]
    section = doc.args.get("section")
    return {
        "record": found.record_id,
        "version": found.version,
        "format": fmt,
        **({} if section is None else {"section": int(section)}),
        **table.header,
        "columns": list(table.columns),
        "roles": dict(table.roles),
        "offset": 0,
        "total": table.total,
        "next_offset": None,
        "rows": rows,
    }


def listing_view(data: Mapping[str, Any]) -> dict[str, Any]:
    """``list_inputs`` with each input under ``record``."""
    rows = [
        {"record": i["ref"], **{k: v for k, v in i.items() if k != "ref"}}
        for i in data.get("inputs", ())
    ]
    return {**data, "inputs": rows}


def search_view(
    args: Mapping[str, Any], data: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``search_inputs`` as the rules read a search: the terms, and each input hit once."""
    terms = list(args.get("terms") or str(args.get("query", "")).split())
    legacy_args: dict[str, Any] = {"terms": terms}
    if args.get("formats"):
        legacy_args["formats"] = list(args["formats"])
    first: dict[str, Mapping[str, Any]] = {}
    for h in data.get("hits", ()):
        first.setdefault(str(h["ref"]), h)
    hits = [
        {"record": r, "snippet": first[r]["snippet"]} if r in first else {"record": r}
        for r in data.get("inputs_hit", sorted(first))
    ]
    shown = {
        "terms": terms,
        "records_checked": data.get("inputs_searched", 0),
        "query": data.get("recorded"),
        "hits": hits,
    }
    return legacy_args, shown
