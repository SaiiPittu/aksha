"""The patent agent's source reader: typed views over a folder of synthetic source records.

Every read tool goes through :class:`SourceWorld`. A read returns a *view* of one record:
the fields the agent uses, parsed from the raw file, and the record's version is a hash
of exactly that view (``view_version``). So the lineage of an artifact names the text it
was built from, not the whole file: when a field nobody read changes (a renamed author
line, a company name on a publication), no read moves and nothing reopens. When a field
that was read changes (a date, a new matching publication), that read moves and the
runtime's invalidation reopens exactly the work built on it.

Record IDs are ``<path>#<view>``: ``notes/C1-self-calibration.md#note``,
``contributors/records.csv#rows:C1``, ``publications/SYN-PUB-0001.json#publication``.
A recorded search is ``publications?q=<json>`` and its version hashes the set of hit IDs.

The reader reads only below ``root`` (a world's ``sources/`` folder). Quotes it returns
are exact substrings of the raw file, so a grader can locate them.
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
from pathlib import Path
from typing import Any

SOURCE_NAME = "patent_sources"
COLLECTIONS = (
    "notes",
    "contributors",
    "disclosures",
    "publications",
    "priorities",
    "templates",
)

MONTHS = {
    m: i
    for i, m in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
    )
}


class NotInWorld(LookupError):
    """A record the tool was asked for is not in the world."""


def view_version(data: Any) -> str:
    """The version of a view: a hash of its canonical JSON (sorted keys, no spaces)."""
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "v-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def day_first(text: str) -> str | None:
    """``14 Jan 2026`` as ``2026-01-14``; None when it is not that form."""
    m = re.fullmatch(r"\s*(\d{1,2}) ([A-Z][a-z]{2}) (\d{4})\s*", text)
    if not m or m.group(2) not in MONTHS:
        return None
    return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).isoformat()


def days_between(earlier: str, later: date) -> int:
    """Whole days from the ISO date ``earlier`` to ``later``."""
    return (later - date.fromisoformat(earlier)).days


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.index("\n---\n", 4)
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip("'\"")
    return meta, text[end + 5 :]


def _sections(body: str) -> dict[str, tuple[str, str]]:
    """``## heading`` sections: name (first word lowered) -> (full heading, text)."""
    out: dict[str, tuple[str, str]] = {}
    parts = re.split(r"^## (.+)$", body, flags=re.M)
    for i in range(1, len(parts), 2):
        heading = parts[i].strip()
        name = heading.split("(")[0].strip().lower().split()[0]
        out[name] = (heading, parts[i + 1].strip())
    return out


def _numbered(text: str) -> list[dict[str, Any]]:
    return [
        {"n": int(m.group(1)), "text": m.group(2).strip()}
        for m in re.finditer(r"^(\d+)\. (.+)$", text, flags=re.M)
    ]


def _bullets(text: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^- (.+)$", text, flags=re.M)]


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


def _csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def _raw_csv_lines(text: str) -> dict[str, str]:
    """The raw line of each CSV row, by its first column."""
    return {ln.split(",", 1)[0]: ln for ln in text.splitlines()[1:] if ln}


@dataclass
class SourceWorld:
    """One world's ``sources/`` folder, read as typed views (module doc).

    ``root`` can be switched to another world (``use``): that is how a test, or an
    operator, applies a change set. ``today`` is the date elapsed days are counted to;
    the composition root passes the injected clock's date.
    """

    root: Path | None = None
    today: date | None = None
    reads: list[str] = field(default_factory=list)

    def use(self, root: Path) -> None:
        """Read from ``root`` from now on."""
        self.root = Path(root)

    # --- raw access -------------------------------------------------------------------

    def _path(self, rel: str) -> Path:
        if self.root is None:
            raise NotInWorld("no source folder is configured")
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents:
            raise NotInWorld(f"{rel} is outside the source folder")
        if not p.is_file():
            raise NotInWorld(f"{rel} is not in the source folder")
        return p

    def raw(self, rel: str) -> str:
        """The raw text of ``rel`` under ``root``."""
        return self._path(rel).read_text(encoding="utf-8")

    def exists_anywhere(self, name: str) -> bool:
        """Whether a file called ``name`` is anywhere under ``root``."""
        assert self.root is not None
        return any(p.name == name for p in self.root.rglob("*") if p.is_file())

    def file_naming(self, ref: str) -> bool:
        """Whether any file under ``root`` has ``ref`` in its name (a referenced document)."""
        assert self.root is not None
        low = ref.casefold()
        return any(low in p.name.casefold() for p in self.root.rglob("*") if p.is_file())

    def files(self, collection: str) -> list[str]:
        """The files of ``collection``, sorted, relative to ``root``."""
        assert self.root is not None
        base = self.root / collection
        if not base.is_dir():
            return []
        return sorted(str(p.relative_to(self.root)) for p in base.iterdir() if p.is_file())

    # --- views ------------------------------------------------------------------------

    def candidates(self) -> dict[str, Any]:
        """Every engineering note's candidate ID and title (the portfolio)."""
        out = []
        for rel in self.files("notes"):
            meta, body = _front_matter(self.raw(rel))
            title = re.search(r"^# (.+)$", body, flags=re.M)
            out.append(
                {
                    "candidate": meta.get("candidate", ""),
                    "note": rel,
                    "title": title.group(1).strip() if title else "",
                }
            )
        return {"candidates": out}

    def note_path(self, candidate: str) -> str:
        """The engineering note of ``candidate``."""
        for row in self.candidates()["candidates"]:
            if row["candidate"] == candidate:
                return str(row["note"])
        raise NotInWorld(f"no engineering note for {candidate}")

    def note(self, candidate: str) -> dict[str, Any]:
        """The note's dated record, problem, mechanism, parameters, alternatives, evidence.

        The author line is not part of the view: who contributed comes from the
        contributor records, and a rename of the author changes nothing the agent read.
        """
        rel = self.note_path(candidate)
        meta, body = _front_matter(self.raw(rel))
        secs = _sections(body)
        date_line = re.search(r"^Date: (.+)$", body, flags=re.M)
        title = re.search(r"^# (.+)$", body, flags=re.M)
        mech_heading, mech = secs.get("mechanism", ("Mechanism", ""))
        evidence = secs.get("evidence", ("", ""))[1]
        refs = sorted(set(re.findall(r"\b[\w\-]+\.(?:csv|xlsx|pdf|json|txt)\b", body)))
        return {
            "file": rel,
            "record_id": meta.get("record_id"),
            "candidate": candidate,
            "title": title.group(1).strip() if title else "",
            "date_line": date_line.group(0) if date_line else None,
            "date": day_first(date_line.group(1)) if date_line else None,
            "problem": secs.get("problem", ("", ""))[1],
            "mechanism_heading": mech_heading,
            "mechanism_steps": _numbered(mech),
            "parameters": _table(secs.get("parameters", ("", ""))[1]),
            "alternatives": _bullets(secs.get("alternatives", ("", ""))[1]),
            "evidence": evidence,
            "open_points": _bullets(secs.get("open", ("", ""))[1]),
            "references": [{"name": n, "found": self.exists_anywhere(n)} for n in refs],
        }

    def contributor_rows(self, candidate: str) -> dict[str, Any]:
        """The contributor records for ``candidate``, without names or employers."""
        rel = "contributors/records.csv"
        text = self.raw(rel)
        keep = (
            "record_id",
            "person_id",
            "role",
            "candidate",
            "contributed",
            "date",
            "agreement_ref",
            "source",
        )
        rows = []
        for r in _csv(text):
            if r["candidate"] != candidate:
                continue
            refs = sorted(set(re.findall(r"\b[A-Z]{2,}-\d{4}-\d+\b", r["agreement_ref"])))
            documents = [{"ref": ref, "found": self.file_naming(ref)} for ref in refs]
            rows.append({**{k: r[k] for k in keep}, "agreement_documents": documents})
        return {"file": rel, "rows": rows}

    def people(self, person_ids: Sequence[str]) -> dict[str, Any]:
        """Names, employers and roles of ``person_ids``, from the contributor records."""
        rel = "contributors/records.csv"
        found: dict[str, dict[str, str]] = {}
        for r in _csv(self.raw(rel)):
            if r["person_id"] in person_ids and r["person_id"] not in found:
                found[r["person_id"]] = {
                    "person_id": r["person_id"],
                    "name": r["name"],
                    "employer": r["employer"],
                    "role": r["role"],
                }
        return {"file": rel, "people": [found[p] for p in sorted(found)]}

    def disclosure_rows(self, candidate: str) -> dict[str, Any]:
        """The disclosure history rows for ``candidate``: dates kept as written.

        The audience column and any external link are left out of the view; a linked file
        inside the sources is named so the agent can read it.
        """
        rel = "disclosures/history.csv"
        text = self.raw(rel)
        lines = _raw_csv_lines(text)
        out = []
        for r in _csv(text):
            if r["candidate"] != candidate:
                continue
            src = r["source"]
            linked = src if "/" in src and not src.startswith("http") else None
            out.append(
                {
                    "event_id": r["event_id"],
                    "date": r["date"] or None,
                    "event_type": r["event_type"],
                    "what_was_shown": r["what_was_shown"],
                    "public": r["public"],
                    "by": r["by"],
                    "linked_file": linked,
                    "row_start": lines[r["event_id"]][: len(r["event_id"]) + len(r["date"]) + 4],
                }
            )
        return {"file": rel, "rows": out}

    def document(self, rel: str) -> dict[str, Any]:
        """A linked disclosure document: an email (headers, body) or a record (sections)."""
        text = self.raw(rel)
        if rel.endswith(".eml.txt"):
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
        secs = _sections(body)
        date_line = re.search(r"^Date: (.+)$", body, flags=re.M)
        return {
            "file": rel,
            "type": "record",
            "event_id": meta.get("event_id"),
            "date": day_first(date_line.group(1)) if date_line else None,
            "sections": {k: v[1] for k, v in secs.items()},
        }

    def priorities(self) -> dict[str, Any]:
        """The ranked business priorities, each with the candidate it relates to."""
        rel = "priorities/business-priorities.md"
        _, body = _front_matter(self.raw(rel))
        items = []
        for m in re.finditer(r"^(\d+)\. (\*\*.+?\*\*)(.*)$", body, flags=re.M):
            rel_to = re.search(r"Relates to (C\d+)", m.group(3))
            items.append(
                {
                    "rank": int(m.group(1)),
                    "title": m.group(2),
                    "text": m.group(3).strip(),
                    "relates_to": rel_to.group(1) if rel_to else None,
                }
            )
        return {"file": rel, "items": items}

    def template(self, name: str) -> dict[str, Any]:
        """A blank form template's fields."""
        rel = f"templates/{name}.md"
        meta, body = _front_matter(self.raw(rel))
        fields = []
        for row in _table(body):
            fields.append(
                {
                    "n": int(row["#"]),
                    "field": row["Field"],
                    "required": row["Required"] == "yes",
                    "default_status": row.get("Default status"),
                    "guidance": row.get("Guidance"),
                }
            )
        return {"file": rel, "template_id": meta.get("template_id"), "fields": fields}

    # --- publications -------------------------------------------------------------------

    def publication_ids(self) -> list[str]:
        """Every publication's number, from its file name."""
        return sorted({Path(p).name.split(".")[0] for p in self.files("publications")})

    def publication_path(self, pub: str) -> str:
        """The file of publication ``pub``."""
        for rel in self.files("publications"):
            if Path(rel).name.split(".")[0] == pub:
                return rel
        raise NotInWorld(f"no publication {pub}")

    def publication(self, pub: str) -> dict[str, Any]:
        """One publication: numbers, dates, URL, status wordings verbatim, claims, text.

        The applicant is not part of the view. A field the source does not state is None.
        """
        rel = self.publication_path(pub)
        text = self.raw(rel)
        if rel.endswith(".json"):
            d = json.loads(text)
            return {
                "file": rel,
                "id": d["publication_number"],
                "office": d.get("office"),
                "kind_code": d.get("kind_code"),
                "title": d.get("title"),
                "dates": d.get("dates", {}),
                "priority_claims": d.get("priority_claims", []),
                "url": d.get("source_url"),
                "status": [
                    {"wording": s["wording"], "source": s.get("source"), "as_of": s.get("as_of")}
                    for s in d.get("status_observations", [])
                ],
                "abstract": d.get("abstract", ""),
                "description": d.get("description", ""),
                "claims": d.get("claims", []),
            }
        return self._pdf_publication(rel, text)

    def _pdf_publication(self, rel: str, text: str) -> dict[str, Any]:
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
        claims_block = re.search(r"Claims\n\n(.+?)\n\nLegal status", text, flags=re.S)
        claims = []
        if claims_block:
            for m in re.finditer(r"(\d+)\. (.+?)(?=\n\n\d+\. |\Z)", claims_block.group(1), re.S):
                body = " ".join(m.group(2).split())
                dep = re.match(r"The method of claim (\d+)", body)
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
            "id": head.group(1) if head else Path(rel).name.split(".")[0],
            "office": grab(r"^(SYN-Office-\d)"),
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

    # --- versions: the VersionedSource the runtime asks ----------------------------------

    def view(self, record_id: str) -> Any:
        """The view a record ID names (module doc), recomputed from the current world."""
        path, _, frag = record_id.partition("#")
        name, _, arg = frag.partition(":")
        if name == "portfolio":
            return self.candidates()
        if name == "note":
            meta, _ = _front_matter(self.raw(path))
            return self.note(meta["candidate"])
        if name == "rows" and path.startswith("contributors"):
            return self.contributor_rows(arg)
        if name == "people":
            return self.people(arg.split(","))
        if name == "rows" and path.startswith("disclosures"):
            return self.disclosure_rows(arg)
        if name == "doc":
            return self.document(path)
        if name == "items":
            return self.priorities()
        if name == "template":
            return self.template(arg)
        if name == "publication":
            return self.publication(Path(path).name.split(".")[0])
        raise NotInWorld(f"no view {frag!r}")

    def record_version(self, owner: str, record_id: str) -> str | None:
        """The current version of ``record_id``; None when the record is gone."""
        try:
            return view_version(self.view(record_id))
        except (NotInWorld, KeyError):
            return None

    @property
    def name(self) -> str:
        """The versioned source's declared name."""
        return SOURCE_NAME

    @property
    def collections(self) -> tuple[str, ...]:
        """The record collections it serves."""
        return COLLECTIONS

    def version(self, owner: str) -> str:
        """The whole folder's version: a hash over every file's path and bytes."""
        assert self.root is not None
        h = hashlib.sha256()
        for p in sorted(self.root.rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(self.root)).encode())
                h.update(p.read_bytes())
        return "w-" + h.hexdigest()[:16]

    def run_query(self, owner: str, query: str) -> str:
        """Re-execute a recorded search: the hash of its hit IDs as a set."""
        return query_version(self.search(parse_query(query)))


def query_id(groups: Sequence[Sequence[str]]) -> str:
    """The recorded query's ID: its groups, in canonical JSON."""
    return "publications?q=" + json.dumps([list(g) for g in groups], separators=(",", ":"))


def parse_query(query: str) -> list[list[str]]:
    """Read :func:`query_id` back."""
    return [list(g) for g in json.loads(query.partition("?q=")[2])]


def query_version(hits: Sequence[Mapping[str, Any]]) -> str:
    """A result set's version: the hash of its hit IDs as a sorted set."""
    return view_version(sorted({str(h["id"]) for h in hits}))
