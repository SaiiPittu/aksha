"""How the offline patent model reads what the shared read tools returned (a test double).

The agent reads its inputs with the harness's tools: ``list_inputs`` names each input with
its kind and the run's ``as_of`` date, ``read_document`` returns its numbered lines. A live
model reads those itself and joins them: which rows of a contributor table are one
candidate's, who a person ID names, how many days ago a disclosure was. The offline model
(``fakes.py``) does the same by fixed rules, and this module is the part that turns what the
tools returned into what those rules read:

1. **The inputs, as the tools showed them.** Each Task lists the kinds of input its rules
   use (:data:`KINDS_READ`) and reads them, a table by the rows of its candidate when its
   columns name one (``where``), an email only when a disclosure row links it; the windows
   of each are put back at their ``start`` offsets (:class:`SeenReader`). Until all are
   read, :func:`reads_needed` names the calls still to make.
2. **The same reading the agent's rules make.** A :class:`~.world.SourceWorld` over those
   texts, with the agent's own offline readers run in process (``OfflineReaders``, the rules
   its fast-class aliases are answered by in ``fake`` mode), gives the views the rules were
   written against (:data:`VIEWS`: the candidates, one candidate's note, contributor rows,
   disclosure rows with days to ``as_of``, people, priorities, a linked document).
3. **A fetched page.** :func:`page_view` reads a ``fetch_url`` result the same way, as a
   publication when it is one, else as a document.

The views are computed in the model's own turn, never by a tool, so nothing here is agent
logic: no module of the agent imports it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from aksha.runtime.api import OfflineReaders, ReturnedText

from . import fakes_readers
from .world import OneRecord, SourceWorld, days_between

READERS = {
    "doc_kind": fakes_readers.DocKind(),
    "note_structure": fakes_readers.NoteStructure(),
    "priorities": fakes_readers.Priorities(),
    "template_fields": fakes_readers.TemplateFields(),
    "publication_biblio": fakes_readers.PublicationBiblio(),
    "table_roles": fakes_readers.TableRoles(),
    "candidate_of": fakes_readers.CandidateOf(),
}
"""The agent's offline readers by alias, run in process over a text a tool returned."""
VIEWS = frozenset(
    {
        "list_candidates",
        "read_note",
        "read_contributors",
        "read_people",
        "read_disclosures",
        "read_linked",
        "read_priorities",
    }
)
"""The views the rules ask for, each computed in the model's turn over what it read."""

Results = Sequence[tuple[str, Mapping[str, Any], Mapping[str, Any]]]


def readers() -> OfflineReaders:
    """The offline readers with the agent's declared fields."""
    from .agent import MODEL_ALIASES  # the agent module imports no fake; this is one way

    return OfflineReaders(READERS, MODEL_ALIASES)


def text_of(windows: Sequence[Mapping[str, Any]]) -> str:
    """A document's text from its windows, each at its ``start``; blanks where none was shown.

    A ``where`` read (a table's header and some rows) is its lines joined: the part itself.
    """
    if any(w.get("where") for w in windows):
        lines = sorted({int(n): str(t) for w in windows for n, t in w.get("lines", ())}.items())
        return "\n".join(t for _, t in lines) + "\n"
    parts = [
        (int(w.get("start", 0)), "\n".join(str(t) for _, t in w.get("lines", ()))) for w in windows
    ]
    size = max((s + len(t) for s, t in parts), default=0)
    buf = [" "] * size
    for s, t in parts:
        buf[s : s + len(t)] = list(t)
    return "".join(buf)


class SeenReader:
    """A record reader over the inputs the model read (module doc, item 1)."""

    def __init__(self, texts: Mapping[str, ReturnedText]) -> None:
        """Serve ``texts`` by record ID."""
        self.texts = dict(texts)

    def read(self, source: str, record_id: str) -> ReturnedText | None:
        """The text read of ``record_id``, if the model read it."""
        return self.texts.get(record_id)

    def records(self, source: str, collection: str) -> tuple[str, ...]:
        """The records the model read, sorted."""
        return tuple(sorted(self.texts))

    def serves(self, source: str) -> bool:
        """Every source: the model's reading is all it has."""
        return True


def _ok(results: Results, tool: str) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    return [(a, r["data"]) for n, a, r in results if n == tool and r.get("status") == "ok"]


TABLE_KINDS = ("contributor_record", "disclosure_record", "meeting_notes", "other")
"""Kinds whose tables (and pipe tables) hold people and events (``world.TABLE_KINDS``)."""
KINDS_READ = {
    "scope": ("invention_disclosure", "meeting_notes", "other"),
    "extract": (
        "invention_disclosure",
        "meeting_notes",
        "contributor_record",
        "disclosure_record",
        "email",
        "business_priorities",
        "other",
    ),
    "prepare": TABLE_KINDS,
}
"""What each Task reads of the inputs, by kind: only what its rules use, so a change to
anything else reopens none of its work."""


def _listing_args(task: str) -> dict[str, Any]:
    return {"kinds": list(KINDS_READ.get(task, KINDS_READ["extract"]))}


def listing(results: Results, task: str = "extract") -> Mapping[str, Any] | None:
    """The latest ``list_inputs`` result of this Task's kinds."""
    want = _listing_args(task)
    got = [d for a, d in _ok(results, "list_inputs") if dict(a) == want]
    return got[-1] if got else None


def _windows(results: Results) -> dict[tuple[str, str], list[Mapping[str, Any]]]:
    out: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for a, d in _ok(results, "read_document"):
        if a.get("section") is None:
            out.setdefault((str(d["ref"]), str(d["version"])), []).append(d)
    return out


def candidate_column(columns: Sequence[str]) -> str | None:
    """The column a table names its candidate in, read from the column names alone."""
    found = [c for c in columns if fakes_readers.guess_role(c, []) == "candidate"]
    return found[0] if len(found) == 1 else None


def _plan(
    task: str, key: str | None, item: Mapping[str, Any], linked: set[str]
) -> dict[str, Any] | None:
    """How one listed input is read in ``task``: whole, by ``where``, or not at all."""
    ref, kind = str(item["ref"]), item.get("kind")
    if task == "extract":
        if kind == "email" and ref not in linked:
            return None  # an email is read when a disclosure row links it
        column = candidate_column(item.get("columns", ())) if item.get("format") == "csv" else None
        if column is not None and key is not None and kind in TABLE_KINDS:
            return {"ref": ref, "where": {"column": column, "values": [key]}}
        if kind == "other" and item.get("format") in ("csv", "json"):
            return None  # a table of no kind that names no candidate is no one's record
    return {"ref": ref}


def reads_needed(
    results: Results, task: str = "extract", key: str | None = None, linked: Sequence[str] = ()
) -> list[tuple[str, dict[str, Any]]]:
    """The calls still to make before this Task's inputs are read (module doc, item 1).

    ``linked`` names the documents the rules asked to read by path.
    """
    listed = listing(results, task)
    if listed is None:
        return [("list_inputs", _listing_args(task))]
    names = [str(i["ref"]) for i in listed.get("inputs", ())]
    wanted = {r for r in names for p in linked if r == p or r.endswith("/" + p.strip("/"))}
    done = [(dict(a), d) for a, d in _ok(results, "read_document")]
    out: list[tuple[str, dict[str, Any]]] = []
    for i in listed.get("inputs", ()):
        plan = _plan(task, key, i, wanted)
        if plan is None:
            continue
        got = [
            d
            for a, d in done
            if a.get("ref") == plan["ref"]
            and a.get("where") == plan.get("where")
            and str(d["version"]) == str(i["version"])
        ]
        if not got:
            out.append(("read_document", plan))
            continue
        shown = {int(w.get("first_line", 1)) for w in got}
        for w in got:
            nxt = w.get("next_offset")
            if nxt is not None and int(nxt) not in shown:
                out.append(("read_document", {**plan, "offset": int(nxt)}))
    if listed.get("next_offset") is not None:
        out.append(("list_inputs", {**_listing_args(task), "offset": int(listed["next_offset"])}))
    return out


def world_of(results: Results, models: OfflineReaders, task: str = "extract") -> SourceWorld:
    """The inputs as read, with the listing's names and its ``as_of`` date (module doc)."""
    listed = listing(results, task) or {}
    current = {(str(i["ref"]), str(i["version"])) for i in listed.get("inputs", ())}
    texts = {
        ref: ReturnedText("inputs", ref, version, text_of(ws))
        for (ref, version), ws in _windows(results).items()
        if (ref, version) in current
    }
    as_of = listed.get("as_of")
    names = tuple(str(i["ref"]) for i in listed.get("inputs", ()))
    return SourceWorld(
        today=date.fromisoformat(str(as_of)) if as_of else None,
        reader=SeenReader(texts),
        models=models,
        names=names,
    )


def _shown(data: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != "parts"}


def listing_query(results: Results, task: str) -> str | None:
    """The ID of this Task's input listing, the scope a claim over what it lists names."""
    listed = listing(results, task)
    read = str((listed or {}).get("query", ""))
    return read.removeprefix("query:").rpartition("@")[0] or None


def view(
    world: SourceWorld, name: str, args: Mapping[str, Any], query: str | None = None
) -> dict[str, Any]:
    """One view the rules ask for (:data:`VIEWS`), as ``{"status", "data"}``.

    ``query`` is the listing the view was worked out from: the candidates name it, so a
    claim over every candidate is scoped to it.
    """
    try:
        data = _view(world, name, args)
        if name == "list_candidates" and query is not None:
            data = {**data, "query": query}
        return {"status": "ok", "data": data}
    except LookupError as e:
        return {"status": "error", "code": "not_found", "message": str(e)}


def _view(world: SourceWorld, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
    if name == "list_candidates":
        return world.candidates()
    if name == "read_note":
        return world.note(str(args["candidate"]))
    if name == "read_contributors":
        return _shown(world.contributor_rows(str(args["candidate"])))
    if name == "read_people":
        return _shown(world.people(sorted(str(p) for p in args["person_ids"])))
    if name == "read_disclosures":
        data = world.disclosure_rows(str(args["candidate"]))
        shown = _shown(data)
        if world.today is not None:  # counted to the listing's as_of date
            shown["as_of"] = world.today.isoformat()
            shown["rows"] = [
                {**r, "elapsed_days": days_between(r["date"], world.today) if r["date"] else None}
                for r in data["rows"]
            ]
        return shown
    if name == "read_linked":
        path = str(args["path"])
        if ".." in path.split("/") or path not in world.records():
            raise LookupError(f"{path} is not among the inputs read")
        return world.document(path)
    if name == "read_priorities":
        return world.priorities()
    raise LookupError(name)


def page_view(data: Mapping[str, Any], models: OfflineReaders) -> dict[str, Any]:
    """A fetched page as a publication when it is one, else as a document (item 3)."""
    text = ReturnedText("web", str(data["ref"]), str(data["version"]), text_of([data]))
    world = SourceWorld(reader=OneRecord(text), models=models)
    record = text.record_id
    found = world.publication_ids()
    if found:
        out = {"kind": "publication", **world.publication(found[0])}
    else:
        out = {"kind": world.kind(record), **world.document(record)}
    return {"url": data.get("url"), "final_url": data.get("final_url"), **out}
