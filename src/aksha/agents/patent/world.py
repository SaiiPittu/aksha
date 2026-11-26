"""The patent agent's input reader: typed views over whatever records the person gave it.

Every read tool goes through :class:`SourceWorld`, which reads the records of the
``inputs`` source through the runtime's record reader and never expects a file name or a
folder. What a record is comes from its content, by rules a patent professional would
apply to any venture's papers:

1. An *invention note* is a Markdown or text record with a mechanism section (a heading
   such as "Mechanism", "Method", "How it works", "Steps" or "Process") holding numbered
   steps. Its candidate ID is its front matter's ``candidate`` (or ``invention``), else a
   ``Candidate:`` line, else the slug of its file name. Its other sections are found by
   what their headings say (problem, parameters, alternatives, evidence, open points).
2. A *contributor table* is a CSV table with a person, a candidate and a contribution
   column; a *disclosure history* is one with a date, what was shown, and whether it was
   public. Columns are recognised by what they are called in practice (``COLUMNS``).
3. *Business priorities* are a Markdown list of ranked bold items (``1. **...**``); an
   item relates to the candidate it names ("Relates to <ID>", or the ID itself).
4. A *form template* is a Markdown table with ``Field`` and ``Required`` columns. An
   invention disclosure form is the one whose title says "disclosure"; the filing
   worksheet is the one whose title says "filing", "worksheet" or "application data".
5. A *publication* is a JSON record with a publication number, or text laid out with the
   bibliographic codes of WIPO Standard ST.9 (``(11)`` number, ``(54)`` title, ``(22)``
   filing date, ``(30)`` priority, ``(45)`` grant date).

A read returns a *view* of one record (or of some rows of a table) and names what it read:
a whole record at its version, or a record part (``where_part``: the rows of one
candidate) at that part's version (``part_version``), so a change to another candidate's
rows reopens nothing built for this one. A search and the candidate listing are recorded
queries (``patent.<tool>?<arguments>``) whose version hashes the set of hits, so a new
matching publication re-executes the search and reopens only the candidate it now matches.

Quotes the views return are exact substrings of the raw record, so a grader can locate
them.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from aksha.runtime.api import RecordReader, ReturnedText

SOURCE_NAME = "inputs"
COLLECTION = "input"
COLLECTIONS = (COLLECTION,)

MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}

HEADINGS: Mapping[str, tuple[str, ...]] = {
    "problem": ("problem", "background", "need", "motivation"),
    "mechanism": ("mechanism", "method", "how it works", "steps", "process", "procedure"),
    "parameters": ("parameters", "settings", "values", "constants", "configuration"),
    "alternatives": ("alternatives", "options", "considered", "rejected", "what we tried"),
    "evidence": ("evidence", "status", "testing", "test results", "validation", "results"),
    "open": ("open", "unresolved", "questions", "to do", "todo"),
}
"""What an invention note's section headings say, by the role of the section."""

COLUMNS: Mapping[str, tuple[str, ...]] = {
    "record_id": ("record_id", "row_id", "id"),
    "person_id": ("person_id", "person", "contributor_id", "contributor"),
    "name": ("name", "person_name", "full_name"),
    "employer": ("employer", "organisation", "organization", "company", "affiliation"),
    "role": ("role", "title", "position"),
    "candidate": ("candidate", "candidate_id", "invention", "invention_id", "project"),
    "contributed": ("contributed", "contribution", "what_they_did", "work"),
    "date": ("date", "on", "when"),
    "agreement_ref": ("agreement_ref", "agreement", "ip_terms", "terms"),
    "source": ("source", "link", "file", "evidence", "record"),
    "event_id": ("event_id", "event", "disclosure_id"),
    "event_type": ("event_type", "type", "kind", "channel"),
    "what_was_shown": ("what_was_shown", "shown", "what", "summary", "description"),
    "public": ("public", "is_public"),
    "by": ("by", "who", "presenter", "presenters", "people"),
}
"""The names each column goes by in a contributor table or a disclosure history."""

TEMPLATE_KINDS: Mapping[str, tuple[str, ...]] = {
    "disclosure": ("disclosure",),
    "worksheet": ("filing", "worksheet", "application data"),
}
"""Which form a template is, by the words of its title or ID."""


class NotInWorld(LookupError):
    """A record the tool was asked for is not among the inputs."""


def query_version(hits: Sequence[Any]) -> str:
    """A recorded query's version: the hash of its hits as a sorted set."""
    text = json.dumps(sorted({json.dumps(h, sort_keys=True) for h in hits}), separators=(",", ":"))
    return "q-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def query_id(tool: str, args: Mapping[str, Any]) -> str:
    """The recorded query's ID: ``patent.<tool>?<canonical JSON arguments>``."""
    return f"patent.{tool}?" + json.dumps(dict(args), sort_keys=True, separators=(",", ":"))


def day_first(text: str) -> str | None:
    """``14 Jan 2026`` (or an ISO date) as ``2026-01-14``; None when it is neither."""
    t = text.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        return t
    m = re.fullmatch(r"(\d{1,2}) ([A-Z][a-z]{2})[a-z]* (\d{4})", t)
    if not m or m.group(2) not in MONTHS:
        return None
    return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).isoformat()


def days_between(earlier: str, later: date) -> int:
    """Whole days from the ISO date ``earlier`` to ``later``."""
    return (later - date.fromisoformat(earlier)).days


def file_name(record: str) -> str:
    """The last segment of a record ID."""
    return record.rsplit("/", 1)[-1]


def _stem(record: str) -> str:
    name = file_name(record)
    for ext in (".pdf.txt", ".eml.txt", ".markdown", ".md", ".txt", ".json", ".csv", ".eml"):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        k, _, v = line.partition(":")
        meta[k.strip().lower()] = v.strip().strip("'\"")
    return meta, text[end + 5 :]


def _role(heading: str) -> str | None:
    low = heading.strip().lower()
    for role, words in HEADINGS.items():
        if any(low.startswith(w) for w in words):
            return role
    return None


def _sections(body: str) -> dict[str, tuple[str, str]]:
    """``## heading`` sections by role (:data:`HEADINGS`): role -> (full heading, text)."""
    out: dict[str, tuple[str, str]] = {}
    parts = re.split(r"^#{2,3} (.+)$", body, flags=re.M)
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        role = _role(heading)
        if role is not None and role not in out:
            out[role] = (heading, parts[i + 1].strip())
    return out


def _numbered(text: str) -> list[dict[str, Any]]:
    return [
        {"n": int(m.group(1)), "text": m.group(2).strip()}
        for m in re.finditer(r"^(\d+)[.)] (.+)$", text, flags=re.M)
    ]


def _bullets(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^[-*] (.+)$", text, flags=re.M)]


def _table(text: str) -> list[dict[str, str]]:
    rows = [ln for ln in text.splitlines() if ln.startswith("|")]
    if len(rows) < 2:
        return []
    head = [c.strip() for c in rows[0].strip("|").split("|")]
    out = []
    for ln in rows[2:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        row = dict(zip(head, cells, strict=False))
        row["row_text"] = ln[: ln.rfind("|")].rstrip() if ln.endswith("|") else ln
        out.append(row)
    return out


def _fold(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def column_map(header: Sequence[str]) -> dict[str, str]:
    """Each role of :data:`COLUMNS` to the header column that holds it, first match wins."""
    folded = {_fold(c): c for c in header}
    out: dict[str, str] = {}
    taken: set[str] = set()
    for role, names in COLUMNS.items():
        for n in names:
            col = folded.get(n)
            if col is not None and col not in taken:
                out[role] = col
                taken.add(col)
                break
    return out


def _param(row: Mapping[str, str]) -> dict[str, str]:
    """A parameter row under the canonical names ``Parameter``, ``Value`` and ``Unit``."""
    by = {_fold(k): v for k, v in row.items() if k != "row_text"}
    name = by.get("parameter") or by.get("name") or by.get("setting") or ""
    unit = by.get("unit") or by.get("units") or ""
    return {**row, "Parameter": name, "Value": by.get("value", ""), "Unit": unit}


def _row_lines(table: Table) -> list[str]:
    """Each data row's raw line, in row order, whatever column comes first.

    A row is one line (as for record parts, :mod:`aksha.runtime.grounding.parts`); blank
    lines, which hold no row, are skipped as the CSV reader skips them.
    """
    return [ln for ln in table.text.splitlines()[1:] if ln.strip()]


def _cell_ends(line: str) -> list[int]:
    """The raw end offset of each cell of one CSV line (a quoted comma is inside a cell)."""
    ends, quoted = [], False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == "," and not quoted:
            ends.append(i)
    ends.append(len(line))
    return ends


def _row_start(table: Table, line: str) -> str:
    """The row's raw text through what identifies it: its ID, date and candidate cells.

    A citation of the row is then tight to the cells that say which event it is, wherever
    those columns sit; the whole line when the table names none of them.
    """
    cols = [table.roles[k] for k in ("event_id", "date", "candidate") if k in table.roles]
    at = [table.header.index(c) for c in cols if c in table.header]
    ends = _cell_ends(line)
    if not at or max(at) >= len(ends):
        return line
    return line[: ends[max(at)]]


@dataclass
class Table:
    """One CSV input: its record, version, text, header, roles and rows."""

    record: str
    version: str
    text: str
    header: list[str]
    roles: dict[str, str]
    rows: list[dict[str, str]]

    def get(self, row: Mapping[str, str], role: str) -> str:
        """The row's value for ``role``; empty when the table has no such column."""
        col = self.roles.get(role)
        return "" if col is None else str(row.get(col, ""))


@dataclass
class SourceWorld:
    """The inputs, read as typed views through the runtime's record reader (module doc).

    ``today`` is the date elapsed days are counted to; the run's recorded start date when
    not given (``ToolContext.run_date``).
    """

    today: date | None = None
    reader: RecordReader | None = None
    _cache: dict[str, ReturnedText] = field(default_factory=dict)

    def over(self, reader: RecordReader | None, run_date: str | None = None) -> SourceWorld:
        """This world reading through ``reader``, counting days to ``today`` or the run date."""
        today = self.today or (date.fromisoformat(run_date) if run_date else None)
        return SourceWorld(today=today, reader=reader)

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

    def resolve(self, ref: str) -> str | None:
        """The record a reference in another record names (a relative path), or None."""
        ref = ref.strip().strip("/")
        if not ref or ref.startswith(("http://", "https://")) or "/" not in ref:
            return None
        return next((r for r in self.records() if r == ref or r.endswith("/" + ref)), None)

    def exists_anywhere(self, name: str) -> bool:
        """Whether an input's file is called ``name``."""
        return any(file_name(r) == name for r in self.records())

    def file_naming(self, ref: str) -> bool:
        """Whether any input's file name holds ``ref`` (a referenced document)."""
        low = ref.casefold()
        return any(low in file_name(r).casefold() for r in self.records())

    def _texts(self, *exts: str) -> list[str]:
        return [r for r in self.records() if file_name(r).lower().endswith(exts)]

    def _tables(self) -> list[Table]:
        out = []
        for r in self._texts(".csv"):
            got = self.found(r)
            rows = list(csv.DictReader(io.StringIO(got.text)))
            header = next(csv.reader(io.StringIO(got.text)), [])
            out.append(Table(r, got.version, got.text, header, column_map(header), rows))
        return out

    def contributor_tables(self) -> list[Table]:
        """Tables with a person, a candidate and a contribution column."""
        need = ("person_id", "candidate", "contributed")
        return [t for t in self._tables() if all(k in t.roles for k in need)]

    def history_tables(self) -> list[Table]:
        """Tables with a date, what was shown and whether it was public."""
        need = ("date", "what_was_shown", "public")
        return [t for t in self._tables() if all(k in t.roles for k in need)]

    # --- invention notes ----------------------------------------------------------------

    def _note_id(self, record: str, meta: Mapping[str, str], body: str) -> str:
        for key in ("candidate", "invention", "candidate_id", "invention_id"):
            if meta.get(key):
                return meta[key]
        line = re.search(r"^(?:Candidate|Invention)(?: ID)?: ([^\s,]+)", body, flags=re.M)
        if line:
            return line.group(1)
        return re.sub(r"[^A-Za-z0-9]+", "-", _stem(record)).strip("-")

    def candidates(self) -> dict[str, Any]:
        """Every invention note's candidate ID, record and title (the portfolio)."""
        out = []
        for r in self._texts(".md", ".markdown", ".txt"):
            meta, body = _front_matter(self.raw(r))
            secs = _sections(body)
            if "mechanism" not in secs or not _numbered(secs["mechanism"][1]):
                continue
            title = re.search(r"^# (.+)$", body, flags=re.M)
            out.append(
                {
                    "candidate": self._note_id(r, meta, body),
                    "note": r,
                    "title": title.group(1).strip() if title else _stem(r),
                }
            )
        return {"candidates": sorted(out, key=lambda c: (c["candidate"], c["note"]))}

    def note_path(self, candidate: str) -> str:
        """The invention note of ``candidate``."""
        for row in self.candidates()["candidates"]:
            if row["candidate"] == candidate:
                return str(row["note"])
        raise NotInWorld(f"no invention note for {candidate}")

    def note(self, candidate: str) -> dict[str, Any]:
        """The note's dated record, problem, mechanism, parameters, alternatives, evidence.

        Who contributed comes from the contributor records, never the note's author line.
        """
        rel = self.note_path(candidate)
        meta, body = _front_matter(self.raw(rel))
        secs = _sections(body)
        date_line = re.search(r"^Date: (.+)$", body, flags=re.M)
        title = re.search(r"^# (.+)$", body, flags=re.M)
        mech_heading, mech = secs.get("mechanism", ("Mechanism", ""))
        evidence = secs.get("evidence", ("", ""))[1]
        refs = sorted(set(re.findall(r"\b[\w\-]+\.(?:csv|xlsx|pdf|json|txt|docx)\b", body)))
        when = day_first(date_line.group(1)) if date_line else day_first(meta.get("date", ""))
        return {
            "file": rel,
            "record_id": meta.get("record_id"),
            "candidate": candidate,
            "title": title.group(1).strip() if title else _stem(rel),
            "date_line": date_line.group(0) if date_line else None,
            "date": when,
            "problem": secs.get("problem", ("", ""))[1],
            "mechanism_heading": mech_heading,
            "mechanism_steps": _numbered(mech),
            "parameters": [_param(p) for p in _table(secs.get("parameters", ("", ""))[1])],
            "alternatives": _bullets(secs.get("alternatives", ("", ""))[1]),
            "evidence": evidence,
            "open_points": _bullets(secs.get("open", ("", ""))[1]),
            "references": [{"name": n, "found": self.exists_anywhere(n)} for n in refs],
        }

    # --- tables -------------------------------------------------------------------------

    def contributor_rows(self, candidate: str) -> dict[str, Any]:
        """The contributor records for ``candidate``, without names or employers."""
        keep = ("record_id", "person_id", "role", "candidate", "contributed", "date",
                "agreement_ref", "source")  # fmt: skip
        rows, parts = [], []
        for t in self.contributor_tables():
            mine = [r for r in t.rows if t.get(r, "candidate") == candidate]
            parts.append((t.record, t.roles["candidate"], candidate))
            for r in mine:
                agreement = t.get(r, "agreement_ref")
                refs = sorted(
                    set(re.findall(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{4}-\d+\b", agreement))
                )
                documents = [{"ref": ref, "found": self.file_naming(ref)} for ref in refs]
                row = {k: t.get(r, k) for k in keep}
                rows.append({**row, "file": t.record, "agreement_documents": documents})
        return {"file": parts[0][0] if parts else None, "rows": rows, "parts": parts}

    def people(self, person_ids: Sequence[str]) -> dict[str, Any]:
        """Names, employers and roles of ``person_ids``, from the contributor records."""
        found: dict[str, dict[str, str]] = {}
        parts = []
        for t in self.contributor_tables():
            parts.append((t.record, t.roles["person_id"], ",".join(sorted(person_ids))))
            for r in t.rows:
                pid = t.get(r, "person_id")
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
            if "candidate" not in t.roles:
                continue
            parts.append((t.record, t.roles["candidate"], candidate))
            for r, line in zip(t.rows, _row_lines(t), strict=False):
                if t.get(r, "candidate") != candidate:
                    continue
                eid = t.get(r, "event_id")
                when = t.get(r, "date")
                out.append(
                    {
                        "event_id": eid,
                        "date": day_first(when) if when else None,
                        "event_type": t.get(r, "event_type"),
                        "what_was_shown": t.get(r, "what_was_shown"),
                        "public": t.get(r, "public"),
                        "by": t.get(r, "by"),
                        "linked_file": self.resolve(t.get(r, "source")),
                        "file": t.record,
                        "row_start": _row_start(t, line),
                    }
                )
        return {"file": parts[0][0] if parts else None, "rows": out, "parts": parts}

    def document(self, rel: str) -> dict[str, Any]:
        """A linked disclosure document: an email (headers, body) or a record (sections)."""
        text = self.raw(rel)
        if re.search(r"^From: .+$", text, flags=re.M) and re.search(r"^Date: ", text, re.M):
            head, _, body = text.partition("\n\n")
            date_h = re.search(r"^Date: (.+)$", head, flags=re.M)
            iso = None
            if date_h:
                m = re.search(r"(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})", date_h.group(1))
                iso = day_first(" ".join(m.groups())) if m else None
            return {
                "file": rel,
                "type": "email",
                "date_header": date_h.group(0) if date_h else None,
                "date": iso,
                "body": body.strip(),
            }
        meta, body = _front_matter(text)
        parts = re.split(r"^## (.+)$", body, flags=re.M)
        secs = {
            parts[i].strip().lower().split()[0]: parts[i + 1].strip()
            for i in range(1, len(parts), 2)
        }
        date_line = re.search(r"^Date: (.+)$", body, flags=re.M)
        return {
            "file": rel,
            "type": "record",
            "event_id": meta.get("event_id"),
            "date": day_first(date_line.group(1)) if date_line else None,
            "sections": secs,
        }

    def priorities(self) -> dict[str, Any]:
        """The ranked business priorities, each with the candidate it relates to."""
        ids = [c["candidate"] for c in self.candidates()["candidates"]]
        for rel in self._texts(".md", ".markdown", ".txt"):
            _, body = _front_matter(self.raw(rel))
            found = list(re.finditer(r"^(\d+)\. (\*\*.+?\*\*)(.*)$", body, flags=re.M))
            if not found or not re.search(r"priorit", body[:400], re.I):
                continue
            items = []
            for m in found:
                said = re.search(r"Relates to ([\w-]+)", m.group(3))
                named = [c for c in ids if re.search(rf"\b{re.escape(c)}\b", m.group(0))]
                items.append(
                    {
                        "rank": int(m.group(1)),
                        "title": m.group(2),
                        "text": m.group(3).strip(),
                        "relates_to": said.group(1) if said else (named[0] if named else None),
                    }
                )
            return {"file": rel, "items": items}
        return {"file": None, "items": []}

    def templates(self) -> dict[str, Any]:
        """Every form template among the inputs: its ID, which form it is, and its record."""
        out = []
        for rel in self._texts(".md", ".markdown"):
            meta, body = _front_matter(self.raw(rel))
            rows = _table(body)
            if not rows or "Field" not in rows[0] or "Required" not in rows[0]:
                continue
            title = re.search(r"^# (.+)$", body, flags=re.M)
            tid = meta.get("template_id") or _stem(rel)
            words = f"{tid} {title.group(1) if title else ''}".lower()
            kind = next(
                (k for k, ws in TEMPLATE_KINDS.items() if any(w in words for w in ws)), None
            )
            out.append({"template": tid, "form": kind, "file": rel})
        return {"templates": sorted(out, key=lambda t: t["template"])}

    def template(self, name: str) -> dict[str, Any]:
        """A blank form template's fields."""
        rel = next(
            (t["file"] for t in self.templates()["templates"] if t["template"] == name), None
        )
        if rel is None:
            raise NotInWorld(f"no form template {name!r} among the inputs")
        meta, body = _front_matter(self.raw(rel))
        fields = []
        for i, row in enumerate(_table(body), start=1):
            n = row.get("#", "")
            fields.append(
                {
                    "n": int(n) if n.isdigit() else i,
                    "field": row["Field"],
                    "required": row["Required"].lower() in ("yes", "y", "true", "required"),
                    "default_status": row.get("Default status"),
                    "guidance": row.get("Guidance"),
                }
            )
        return {"file": rel, "template_id": meta.get("template_id") or name, "fields": fields}

    # --- publications -------------------------------------------------------------------

    def _publication_records(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for rel in self.records():
            name = file_name(rel).lower()
            text = self.raw(rel)
            if name.endswith(".json"):
                try:
                    d = json.loads(text)
                except ValueError:
                    continue
                num = (
                    d.get("publication_number") or d.get("publication")
                    if isinstance(d, dict)
                    else None
                )
                if num and isinstance(d, dict) and ("claims" in d or "title" in d):
                    out[str(num)] = rel
            elif name.endswith(".txt") and re.search(r"\(11\) \S+", text):
                head = re.search(r"\(11\) (\S+)", text)
                if head:
                    out[head.group(1)] = rel
        return out

    def publication_ids(self) -> list[str]:
        """Every publication's number, from its own content."""
        return sorted(self._publication_records())

    def publication_path(self, pub: str) -> str:
        """The record of publication ``pub``."""
        found = self._publication_records().get(pub)
        if found is None:
            raise NotInWorld(f"no publication {pub}")
        return found

    def publication(self, pub: str) -> dict[str, Any]:
        """One publication: numbers, dates, URL, status wordings verbatim, claims, text.

        The applicant is not part of the view. A field the source does not state is None.
        """
        rel = self.publication_path(pub)
        text = self.raw(rel)
        if rel.lower().endswith(".json"):
            d = json.loads(text)
            return {
                "file": rel,
                "id": d.get("publication_number") or d.get("publication"),
                "office": d.get("office"),
                "kind_code": d.get("kind_code"),
                "title": d.get("title"),
                "dates": d.get("dates", {}),
                "priority_claims": d.get("priority_claims", []),
                "url": d.get("source_url") or d.get("url"),
                "status": [
                    {"wording": s["wording"], "source": s.get("source"), "as_of": s.get("as_of")}
                    for s in d.get("status_observations", d.get("status", []))
                    if isinstance(s, dict)
                ],
                "abstract": d.get("abstract", ""),
                "description": d.get("description", ""),
                "claims": d.get("claims", []),
            }
        return self._st9_publication(rel, text)

    def _st9_publication(self, rel: str, text: str) -> dict[str, Any]:
        """A publication laid out with WIPO ST.9 codes (module doc, item 5)."""

        def grab(pattern: str) -> str | None:
            m = re.search(pattern, text, flags=re.M)
            return m.group(1).strip() if m else None

        head = re.search(r"\(11\) (\S+) (\S+)", text)
        prio = re.search(r"\(30\) Priority: (\S+), (\S+)", text)
        status = []
        s = re.search(r"^(Status: .+)$", text, flags=re.M)
        if s:
            status.append({"wording": s.group(1), "source": "document front page", "as_of": None})
        legal = re.search(r"^Legal status \(register extract, ([\d-]+)\): (.+)$", text, flags=re.M)
        if legal:
            status.append(
                {
                    "wording": legal.group(2).strip(),
                    "source": "register extract",
                    "as_of": legal.group(1),
                }
            )
        desc = re.search(r"Description\n\n(.+?)\n\n", text, flags=re.S)
        claims_block = re.search(
            r"Claims\n\n(.+?)(?:\n\n(?:Legal status|Page \d)|\s*\Z)", text, flags=re.S
        )
        claims = []
        if claims_block:
            for m in re.finditer(r"(\d+)\. (.+?)(?=\n\n\d+\. |\Z)", claims_block.group(1), re.S):
                body = " ".join(m.group(2).split())
                dep = re.match(r"The [a-z][\w -]*? of claim (\d+)", body)
                claims.append(
                    {
                        "number": int(m.group(1)),
                        "text": body,
                        "depends_on": int(dep.group(1)) if dep else None,
                    }
                )
        title = grab(r"^\(54\) (.+)$")
        return {
            "file": rel,
            "id": head.group(1) if head else _stem(rel),
            "office": grab(r"^(\S.*?)\s{2,}\(11\)"),
            "kind_code": head.group(2) if head else None,
            "title": title,
            "dates": {
                k: v
                for k, v in (
                    ("priority", prio.group(2) if prio else None),
                    ("filing", grab(r"\(22\) Filing date: (\S+)")),
                    ("grant", grab(r"\(45\) Date of grant: (\S+)")),
                )
                if v
            },
            "priority_claims": [{"number": prio.group(1), "date": prio.group(2)}] if prio else [],
            "url": None,
            "status": status,
            "abstract": "",
            "description": " ".join(desc.group(1).split()) if desc else "",
            "claims": claims,
        }

    def search(self, groups: Sequence[Sequence[str]]) -> list[dict[str, Any]]:
        """Publications whose text matches every group (any one term of a group)."""
        hits = []
        for pub in self.publication_ids():
            p = self.publication(pub)
            hay = " ".join(
                [
                    p["title"] or "",
                    p["abstract"],
                    p["description"],
                    *(c["text"] for c in p["claims"]),
                ]
            ).casefold()
            if all(any(t.casefold() in hay for t in g) for g in groups):
                hits.append({"id": p["id"], "title": p["title"]})
        return hits
