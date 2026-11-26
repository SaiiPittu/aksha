"""The patent agent's offline fast-class models: rule-based readers, test doubles only.

Each implements one alias's contract (``world.py``) from the passages or the state it is
given, and nothing else, so ``fake`` mode is offline and repeatable. They are general rules
a patent paralegal applies to any venture's papers, written for any layout: headings in
Markdown, bold, a short line ending in a colon or a slide title; steps numbered, bulleted
or written as sentences; tables of any column names; forms as tables or lists; publications
as JSON, front-page codes (WIPO ST.9), labelled text or XML. None names a fixture. A live
model replaces every one of them.
"""

from __future__ import annotations

import itertools
import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aksha.runtime.api import (
    ClassAnswer,
    ClassificationRequest,
    ClassificationResult,
    ExtractionRequest,
    ExtractionResult,
    FieldAnswer,
    FieldSpec,
    QuestionSpec,
    RuleClassificationModel,
    RuleExtractionModel,
    SpanRef,
    distribution_ppm,
    find_dates,
    parse_date,
    ppm,
)

SURE = Decimal("0.95")
ABSENT = ppm(Decimal("0.9"))

HEADINGS: dict[str, tuple[str, ...]] = {
    "problem": ("problem", "background", "need", "motivation", "context", "pain", "why"),
    "mechanism": ("mechanism", "method", "how it works", "how it would work", "how it will work",
                  "how we do it", "steps", "process", "proposal",
                  "procedure", "detailed description", "description of the invention",
                  "embodiment", "approach", "the idea", "solution", "algorithm", "design",
                  "technique", "proposed method"),
    "parameters": ("parameters", "settings", "values", "constants", "configuration",
                   "key numbers", "tuning", "numbers"),
    "alternatives": ("alternatives", "options", "considered", "rejected", "what we tried",
                     "other approaches", "discarded"),
    "evidence": ("evidence", "status", "testing", "test results", "validation", "results",
                 "where it stands", "maturity", "build status", "trials"),
    "open": ("open", "unresolved", "questions", "to do", "todo", "next steps", "gaps",
             "action items", "follow-ups", "follow ups"),
}  # fmt: skip
"""What a section's heading says, by the role of the section."""

_MD = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD = re.compile(r"^\s*(?:\*\*|__)(.{2,80}?)(?:\*\*|__)\s*:?\s*$")
_SLIDE = re.compile(r"^\s*(?:slide|page)\s+\d+\s*[:.\u2013\u2014-]\s*(.+?)\s*$", re.I)
_COLON = re.compile(r"^\s*([A-Z][^.!?:|]{1,60}):\s*$")
_ITEM = re.compile(r"^\s*([-*+•▪◦]|\d{1,3}[.)]|[a-z][.)]|\(\w{1,3}\))\s+")
_KV = re.compile(r"^\s*([A-Za-z][A-Za-z _-]{0,30}):[ \t]*(.+?)\s*$")


@dataclass(frozen=True)
class Line:
    """One passage, parsed: what it is, its body's offsets inside the passage."""

    span: str
    text: str
    kind: str  # heading, item, row, meta, paragraph
    level: int
    body: tuple[int, int]
    marker: str = ""


def parse_line(span: str, text: str) -> Line:
    """What a passage is, by its marks, and where its body sits."""
    stripped = text.strip()
    lead = len(text) - len(text.lstrip())
    for pattern, level in ((_MD, 0), (_SLIDE, 2), (_BOLD, 3), (_COLON, 3)):
        m = pattern.match(text)
        if m:
            g = m.lastindex or 1
            lv = len(m.group(1)) if pattern is _MD else level
            body = m.group(2) if pattern is _MD else m.group(g)
            at = text.find(body, m.start(g))
            return Line(span, text, "heading", lv, (at, at + len(body)))
    if stripped.startswith("|"):
        return Line(span, text, "row", 0, (lead, len(text.rstrip())))
    m = _ITEM.match(text)
    if m and "\n" not in stripped[:2]:
        return Line(span, text, "item", 0, (m.end(), len(text.rstrip())), m.group(1))
    m = _KV.match(text)
    if m and "\n" not in text and len(stripped) < 160:
        return Line(span, text, "meta", 0, (m.start(2), m.end(2)), m.group(1).strip().lower())
    return Line(span, text, "paragraph", 0, (lead, len(text.rstrip())))


def role_of(heading: str) -> str | None:
    """The section role a heading names."""
    low = heading.strip().strip("*_").lower()
    for role, words in HEADINGS.items():
        if any(low == w or low.startswith(w) or f" {w}" in f" {low}" for w in words):
            return role
    return None


class _Found:
    def __init__(self) -> None:
        self.items: dict[str, list[tuple[str, SpanRef]]] = {}

    def add(self, name: str, value: str, span: str, s: int, e: int) -> None:
        while s < e and value[:1].isspace():
            value, s = value[1:], s + 1
        if e <= s:
            return
        got = self.items.setdefault(name, [])
        if not any(r.span_id == span and r.start == s and r.end == e for _, r in got):
            got.append((value, SpanRef(span, s, e)))

    def has(self, name: str) -> bool:
        return bool(self.items.get(name))

    def answer(self, spec: FieldSpec) -> FieldAnswer:
        got = self.items.get(spec.name, [])
        if not got:
            return FieldAnswer(None, (), p_null_ppm=ABSENT)
        sure, unsure = ppm(SURE), ppm(1 - SURE)
        if spec.type == "list":
            return FieldAnswer(tuple(v for v, _ in got), tuple(r for _, r in got), unsure, sure)
        value, ref = got[0]
        return FieldAnswer(value, (ref,), unsure, sure)

    def result(self, request: ExtractionRequest) -> ExtractionResult:
        return ExtractionResult(tuple((f.name, self.answer(f)) for f in request.fields))


def _sections(lines: Sequence[Line]) -> list[tuple[str | None, Line | None, list[Line]]]:
    out: list[tuple[str | None, Line | None, list[Line]]] = [(None, None, [])]
    for ln in lines:
        if ln.kind == "heading":
            title = ln.level == 1 and ln.text.lstrip().startswith("#")
            out.append((None if title else role_of(ln.text[ln.body[0] : ln.body[1]]), ln, []))
        else:
            out[-1][2].append(ln)
    return out


def _sentences(text: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    for m in re.finditer(r"[^.!?]+[.!?]?", text[start:end]):
        s, e = start + m.start(), start + m.end()
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if len(text[s:e].split()) >= 4:
            yield s, e


_HOW = re.compile(r"\b(lets|allows|enables|using|uses|works by|snaps onto|so that|so the)\b",
                  re.I)  # fmt: skip
"""Words a plain sentence uses to say how a thing works."""
_MADE = re.compile(r"\b(built|designed|invented|idea|sketched|prototyped?|came up with)\b",
                   re.I)  # fmt: skip
_THING = re.compile(r"\b(?:the|a|an)\s+((?:[\w-]+\s){0,3}[\w-]+?)\s+(?:that|which|she|he|"
                    r"they|we|it)\b", re.I)  # fmt: skip
"""The thing a "how it works" sentence is about: "a pre-cut tread template that snaps"."""
IMPLEMENTATION = (
    ("idea_only", re.compile(r"\b(whiteboard|not yet built|idea only|concept only|on paper|"
                             r"not built|sketch only)\b", re.I)),
    ("built_and_tested", re.compile(r"(?<!not )(?<!not yet )\b(bench|field[- ]tested|"
                                    r"field trial|tested on|test run)\b", re.I)),
    ("in_use_under_nda", re.compile(r"\b(pilot|deployed|in use|running at)\b", re.I)),
    ("prototype", re.compile(r"\b(prototype|proof of concept)\b", re.I)),
)  # fmt: skip
"""How far an invention is built, by the words its evidence uses (first match wins)."""
_REF = re.compile(r"`([^`]+\.[A-Za-z]{2,5})`|\b([\w-]+\.(?:csv|xlsx|xls|pdf|json|txt|docx|pptx))\b")


class NoteStructure(RuleExtractionModel):
    """``note_structure``: an invention note in any layout (module doc)."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """Every field the request names."""
        found = _Found()
        lines = [parse_line(s.span_id, s.text) for s in request.state]
        sections = _sections(lines)
        for role, heading, body in sections:
            if role is None:
                continue
            items = [ln for ln in body if ln.kind in ("item", "row")]
            if role == "mechanism":
                steps = [ln for ln in body if ln.kind == "item"]
                if steps:
                    for ln in steps:
                        found.add("mechanism_steps", ln.text[ln.body[0] : ln.body[1]], ln.span,
                                  *ln.body)  # fmt: skip
                else:
                    for ln in body:
                        if ln.kind == "paragraph":
                            for s, e in _sentences(ln.text, *ln.body):
                                found.add("mechanism_steps", ln.text[s:e], ln.span, s, e)
            elif role in ("alternatives", "open"):
                field = "alternatives" if role == "alternatives" else "open_points"
                src = [ln for ln in body if ln.kind == "item"] or [
                    ln for ln in body if ln.kind == "paragraph"
                ]
                for ln in src:
                    found.add(field, ln.text[ln.body[0] : ln.body[1]], ln.span, *ln.body)
            elif role == "parameters":
                for ln in items:
                    if ln.kind == "row":
                        cells = [c.strip() for c in ln.text.strip().strip("|").split("|")]
                        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                            continue
                        if ln is next((x for x in body if x.kind == "row"), None):
                            continue  # the table's header row
                        line = ln.text.rstrip()
                        end = line.rfind("|") if line.endswith("|") else len(line)
                        found.add("parameters", ln.text[ln.body[0] : end].rstrip(), ln.span,
                                  ln.body[0], ln.body[0] + len(ln.text[ln.body[0] : end].rstrip()))  # fmt: skip  # noqa: E501
                    else:
                        found.add("parameters", ln.text[ln.body[0] : ln.body[1]], ln.span,
                                  *ln.body)  # fmt: skip
            elif role in ("problem", "evidence"):
                first = next((ln for ln in body if ln.kind in ("paragraph", "item")), None)
                if first is not None and not found.has(role):
                    found.add(role, first.text[first.body[0] : first.body[1]], first.span,
                              *first.body)  # fmt: skip
                if role == "evidence":
                    hay = [heading, *body] if heading is not None else list(body)
                    self._status(hay, found)
        if not any(role for role, _, _ in sections):
            self._prose(lines, found)
        if found.has("mechanism_steps"):
            mech = next((h for r, h, _ in sections if r == "mechanism" and h is not None), None)
            if mech is not None and not found.has("implementation_status"):
                self._status([mech], found)
            self._identity(lines, found)
            for ln in lines:
                for m in _REF.finditer(ln.text):
                    g = 1 if m.group(1) else 2
                    found.add("references", m.group(g), ln.span, m.start(g), m.end(g))
        return found.result(request)

    def _prose(self, lines: Sequence[Line], found: _Found) -> None:
        """A plain note with no sections, read as an invention note.

        Each sentence saying how a thing works is a step, the thing it names the title, and
        its words how far it is built. Only a note that says someone made or thought of the
        thing (built, designed, an idea) is read so.
        """
        if not any(_MADE.search(ln.text) for ln in lines):
            return
        for ln in lines:
            if ln.kind not in ("paragraph", "item"):
                continue
            for s, e in _sentences(ln.text, *ln.body):
                sentence = ln.text[s:e]
                if not _HOW.search(sentence):
                    continue
                found.add("mechanism_steps", sentence, ln.span, s, e)
                m = _THING.search(sentence)
                if m and not found.has("title"):
                    found.add("title", m.group(1), ln.span, s + m.start(1), s + m.end(1))
        if found.has("mechanism_steps"):
            self._status(list(lines), found)

    def _status(self, lines: Sequence[Line], found: _Found) -> None:
        for status, pattern in IMPLEMENTATION:
            for ln in lines:
                m = pattern.search(ln.text)
                if m:
                    found.add("implementation_status", status, ln.span, m.start(), m.end())
                    return

    def _identity(self, lines: Sequence[Line], found: _Found) -> None:
        """Candidate ID, title and date from the lines above the first section."""
        phys: list[tuple[str, str, int]] = []
        first_section = None
        for ln in lines:
            if ln.kind == "heading" and ln.level >= 2 and role_of(ln.text[ln.body[0] : ln.body[1]]):
                first_section = len(phys)  # the first section with a role ends the head
                break
            at = 0
            for part in ln.text.split("\n"):
                phys.append((ln.span, part, at))
                at += len(part) + 1
        head = phys if first_section is None else phys[:first_section]
        dated: tuple[str, str, int, int] | None = None
        for span, text, at in head:
            m = _KV.match(text)
            if m is None:
                continue
            key = m.group(1).strip().lower()
            value = m.group(2).strip()
            vs = at + text.find(value, m.start(2))
            clean = value.strip("'\"")
            cs = vs + value.find(clean)
            named = bool(set(re.split(r"[ _-]+", key)) & {"candidate", "invention", "idea",
                                                          "docket"}) or key in ("ref", "reference")  # fmt: skip  # noqa: E501
            if named and _ID_VALUE.match(clean.split()[0].rstrip(".,") if clean else "") \
                    and not found.has("candidate_id"):  # fmt: skip
                token = re.match(r"[A-Za-z0-9][\w.-]*", clean)
                if token:
                    tok = token.group(0).rstrip(".,")
                    found.add("candidate_id", tok, span, cs, cs + len(tok))
            elif key == "title" and not found.has("title"):
                found.add("title", clean, span, cs, cs + len(clean))
            elif key in ("date", "dated", "meeting date", "recorded", "when"):
                dates = find_dates(clean, "dmy")
                if dates:
                    iso, ds, de = dates[0]
                    dated = (iso, span, cs + ds, cs + de)  # the last dated line wins
        if not found.has("candidate_id"):
            for span, text, at in head:  # an ID that leads a line of the head names it
                m = re.match(r"\s*([A-Z]{1,8}-\d{1,6})\b(?!-)", text)
                if m:
                    found.add("candidate_id", m.group(1), span, at + m.start(1), at + m.end(1))
                    break
        if dated is None:
            for span, text, at in head:
                dates = find_dates(text, "dmy")
                if dates:
                    iso, ds, de = dates[0]
                    dated = (iso, span, at + ds, at + de)
                    break
        if dated is not None:
            found.add("date", dated[0], dated[1], dated[2], dated[3])
        if not found.has("title"):
            top = next((ln for ln in lines if ln.kind == "heading" and ln.level == 1), None)
            top = top or next((ln for ln in lines if ln.kind == "heading"), None)
            if top is not None:
                found.add("title", top.text[top.body[0] : top.body[1]], top.span, *top.body)


# --- priorities ---------------------------------------------------------------------------

PRIORITY_WORDS = re.compile(r"\b(priorit\w*|goals?|objectives?|roadmap|focus areas?|OKRs?)\b", re.I)


class Priorities(RuleExtractionModel):
    """``priorities``: the ranked items under a priorities title, in rank order."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """The ``items`` field."""
        found = _Found()
        if self._delimited(request, found):
            return found.result(request)
        lines = [parse_line(s.span_id, s.text) for s in request.state]
        on = False
        rows: list[tuple[int, Line, tuple[int, int]]] = []
        header: list[str] = []
        for ln in lines:
            if ln.kind == "heading":
                on = bool(PRIORITY_WORDS.search(ln.text))
                header = []
                continue
            if not on:
                continue
            if ln.kind == "item" and (ln.marker[:1].isdigit() or not rows):
                found.add("items", ln.text[ln.body[0] : ln.body[1]], ln.span, *ln.body)
            elif ln.kind == "row":
                cells = [c.strip() for c in ln.text.strip().strip("|").split("|")]
                if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    continue
                if not header:
                    header = [c.lower() for c in cells]
                    continue
                rank_at = next((i for i, h in enumerate(header) if h in ("rank", "#", "priority",
                                                                        "order", "no")), None)  # fmt: skip  # noqa: E501
                rank = cells[rank_at] if rank_at is not None and rank_at < len(cells) else ""
                n = int(rank) if rank.isdigit() else len(rows) + 1
                rows.append((n, ln, ln.body))
        for _, ln, (s, e) in sorted(rows, key=lambda r: r[0]):
            found.add("items", ln.text[s:e], ln.span, s, e)
        return found.result(request)

    def _delimited(self, request: ExtractionRequest, found: _Found) -> bool:
        """A delimited table whose header names a rank: each row an item, in rank order."""
        state = list(request.state)
        if not state:
            return False
        head = state[0].text
        delim = next((d for d in ("\t", ";", ",") if d in head and "|" not in head), None)
        if delim is None:
            return False
        names = [c.strip().lower() for c in head.split(delim)]
        rank_at = next((i for i, h in enumerate(names) if h in ("rank", "#", "priority", "order",
                                                               "no", "position")), None)  # fmt: skip  # noqa: E501
        if rank_at is None:
            return False
        rows: list[tuple[int, Any]] = []
        for sp in state[1:]:
            cells = sp.text.split(delim)
            rank = cells[rank_at].strip() if rank_at < len(cells) else ""
            rows.append((int(rank) if rank.isdigit() else len(rows) + 1, sp))
        for _, sp in sorted(rows, key=lambda r: r[0]):
            found.add("items", sp.text, sp.span_id, 0, len(sp.text))
        return bool(rows)


# --- forms --------------------------------------------------------------------------------

FIELD_COLUMN = ("field", "field name", "item", "question", "entry", "data", "name", "section")
REQUIRED_COLUMN = ("required", "mandatory", "must", "obligatory", "req", "required?")
TRUTHY = re.compile(r"^(yes|y|true|required|mandatory|must|x|✓|✔|\*)$", re.I)
FORM_TITLE = re.compile(r"\b(form|template|worksheet|sheet|intake|questionnaire|checklist)\b", re.I)
COUNSEL = re.compile(r"\bcounsel\b|\battorney\b|\blawyer\b", re.I)


def _cells(text: str) -> list[tuple[str, int, int]]:
    out = []
    line = text.rstrip()
    bars = [i for i, ch in enumerate(line) if ch == "|"]
    for a, b in itertools.pairwise(bars):
        cs, ce = a + 1, b
        while cs < ce and line[cs].isspace():
            cs += 1
        while ce > cs and line[ce - 1].isspace():
            ce -= 1
        out.append((line[cs:ce], cs, ce))
    return out


class TemplateFields(RuleExtractionModel):
    """``template_fields``: a blank form as a table of fields, or a list of fields."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """Title, fields, the required ones and the ones only counsel completes."""
        found = _Found()
        lines = [parse_line(s.span_id, s.text) for s in request.state]
        title = next((ln for ln in lines if ln.kind == "heading"), None)
        titled = bool(title is not None and FORM_TITLE.search(title.text))
        header: list[str] | None = None
        for ln in lines:
            if ln.kind == "heading":
                header = None
                continue
            if ln.kind == "row":
                cells = _cells(ln.text)
                if all(re.fullmatch(r":?-{2,}:?", c[0]) for c in cells if c[0]):
                    continue
                if header is None:
                    header = [c[0].strip("* ").lower() for c in cells]
                    continue
                fi = next((i for i, h in enumerate(header) if h in FIELD_COLUMN), None)
                ri = next((i for i, h in enumerate(header) if h in REQUIRED_COLUMN), None)
                if fi is None or (ri is None and not titled) or fi >= len(cells):
                    continue
                name, s, e = cells[fi]
                if not name:
                    continue
                found.add("fields", name, ln.span, s, e)
                if ri is not None and ri < len(cells) and TRUTHY.match(cells[ri][0].strip()):
                    found.add("required", name, ln.span, s, e)
                rest = " ".join(c[0] for i, c in enumerate(cells) if i not in (fi, ri))
                if re.search(r"needs_counsel|counsel (only|to (complete|confirm|decide))", rest,
                             re.I):  # fmt: skip
                    found.add("counsel_only", name, ln.span, s, e)
            elif ln.kind == "item" and titled:
                body = ln.text[ln.body[0] : ln.body[1]]
                m = re.match(r"^\[[ x]\]\s*", body)
                bs = ln.body[0] + (m.end() if m else 0)
                name_m = re.match(r"([^:(\u2013\u2014*_]{2,60}?)\s*(?=\(|:|\*|_{2,}|\s[\u2013\u2014-]\s|$)",  # noqa: E501
                                  ln.text[bs : ln.body[1]])  # fmt: skip
                if name_m is None:
                    continue
                name = name_m.group(1).strip()
                s = bs + ln.text[bs:].find(name)
                found.add("fields", name, ln.span, s, s + len(name))
                if re.search(r"\(required\)|\*\s*$|\brequired\b|\bmandatory\b", body, re.I):
                    found.add("required", name, ln.span, s, s + len(name))
                if COUNSEL.search(body):
                    found.add("counsel_only", name, ln.span, s, s + len(name))
        if found.has("fields") and title is not None:
            found.add("title", title.text[title.body[0] : title.body[1]], title.span, *title.body)
        return found.result(request)


# --- publications -------------------------------------------------------------------------

_NUM_KEYS = ("publication_number", "publication", "pub_number", "patent_number", "doc_number",
             "publication_no", "number")  # fmt: skip


def _flat(d: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(d, dict):
        for k, v in d.items():
            key = f"{prefix}{str(k).lower()}"
            yield key, v
            yield from _flat(v, key + ".")


def _where(text: str, value: str, after: int = 0) -> tuple[int, int] | None:
    m = re.compile('"' + re.escape(json.dumps(value)[1:-1]) + '"').search(text, after)
    return (m.start() + 1, m.end() - 1) if m else None


def _enclosing(text: str, at: int) -> tuple[int, int]:
    """The innermost ``{...}`` of ``text`` around offset ``at``."""
    depth, s = 0, at
    while s > 0:
        s -= 1
        if text[s] == "}":
            depth += 1
        elif text[s] == "{":
            if depth == 0:
                break
            depth -= 1
    depth, e = 0, at
    while e < len(text):
        if text[e] == "{":
            depth += 1
        elif text[e] == "}":
            if depth == 0:
                return s, e + 1
            depth -= 1
        e += 1
    return s, len(text)


class PublicationBiblio(RuleExtractionModel):
    """``publication_biblio``: a patent publication in JSON, ST.9 codes, labels or XML."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """Every bibliographic field the record states."""
        found = _Found()
        whole = request.state[0] if len(request.state) == 1 else None
        if whole is not None and whole.text.lstrip()[:1] in "{[":
            self._json(whole.span_id, whole.text, found)
        else:
            self._text(request.state, found)
        if not found.has("number"):
            return ExtractionResult(
                tuple((f.name, FieldAnswer(None, (), p_null_ppm=ABSENT)) for f in request.fields)
            )
        return found.result(request)

    def _json(self, span: str, text: str, found: _Found) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        flat = dict(_flat(data))
        num = next((flat[k] for k in _NUM_KEYS if isinstance(flat.get(k), str)), None)
        if num is None or not any(k in flat for k in ("claims", "title", "abstract")):
            return

        def put(name: str, value: Any) -> None:
            if isinstance(value, str) and value and (at := _where(text, value)):
                found.add(name, value, span, *at)

        put("number", num)
        for name, keys in (("kind_code", ("kind_code", "kind")), ("office", ("office",
                           "authority", "country")), ("title", ("title", "invention_title")),
                           ("abstract", ("abstract",)), ("description", ("description",)),
                           ("url", ("source_url", "url", "link"))):  # fmt: skip
            put(name, next((flat[k] for k in keys if isinstance(flat.get(k), str)), None))
        for name, words in (("filing_date", ("filing", "filed", "application_date")),
                            ("priority_date", ("priority",)),
                            ("publication_date", ("publication", "published")),
                            ("grant_date", ("grant", "granted"))):  # fmt: skip
            for k, v in flat.items():
                if (
                    isinstance(v, str)
                    and any(w in k.split(".")[-1] for w in words)
                    and parse_date(v[:10])
                    and (at := _where(text, v)) is not None
                ):
                    found.add(name, parse_date(v[:10]) or v, span, *at)
                    break
        for k, v in flat.items():
            last = k.split(".")[-1]
            if "priorit" in last and isinstance(v, list):
                for item in v:
                    n = item.get("number") if isinstance(item, dict) else item
                    if isinstance(n, str) and (at := _where(text, n)):
                        s, e = _enclosing(text, at[0]) if isinstance(item, dict) else at
                        found.add("priority_claims", text[s:e], span, s, e)
            if last in ("status_observations", "status", "legal_status", "statuses") and \
                    isinstance(v, list | str):  # fmt: skip
                for item in v if isinstance(v, list) else [v]:
                    w = (item.get("wording") or item.get("text") or item.get("status")
                         if isinstance(item, dict) else item)  # fmt: skip
                    if isinstance(w, str) and (at := _where(text, w)):
                        found.add("status", w, span, *at)
            if last == "claims" and isinstance(v, list):
                pos = 0
                for item in v:
                    t = item.get("text") if isinstance(item, dict) else item
                    if isinstance(t, str) and (at := _where(text, t, pos)):
                        found.add("claims", t, span, *at)
                        pos = at[1]

    def _text(self, state: Sequence[Any], found: _Found) -> None:
        in_claims = False
        in_desc = False
        for sp in state:
            t = sp.text
            for m in re.finditer(r"\(11\)\s*(\S+)(?:\s+([A-Z]\d?))?", t):
                found.add("number", m.group(1), sp.span_id, *m.span(1))
                if m.group(2):
                    found.add("kind_code", m.group(2), sp.span_id, *m.span(2))
                office = re.match(r"^\s*(\S.*?)\s{2,}\(11\)", t[: m.end()].split("\n")[-1])
                if office:
                    line_at = t.rfind("\n", 0, m.start()) + 1
                    found.add("office", office.group(1), sp.span_id, line_at + office.start(1),
                              line_at + office.end(1))  # fmt: skip
            for m in re.finditer(r"\b(?i:Publication|Pub\.|Patent|Document)\s*(?i:No\.?|Number)"
                                 r"\s*[:#]?\s*([A-Z]{2,}[\w-]*\d[\w-]*)(?:[ \t]+([A-Z]\d?)\b)?",
                                 t):  # fmt: skip
                found.add("number", m.group(1), sp.span_id, *m.span(1))
                if m.group(2):
                    found.add("kind_code", m.group(2), sp.span_id, *m.span(2))
            for m in re.finditer(r"^\s*(?:Office|Issuing office|Authority)\s*:\s*(.+?)\s*$", t,
                                 re.M | re.I):  # fmt: skip
                found.add("office", m.group(1), sp.span_id, *m.span(1))
            for m in re.finditer(r"<(doc-number|publication-number)>([^<]+)</\1>", t):
                found.add("number", m.group(2), sp.span_id, *m.span(2))
            for pat, name in ((r"\(54\)\s*(.+)", "title"), (r"^\s*Title\s*:\s*(.+)", "title"),
                              (r"<invention-title[^>]*>([^<]+)<", "title")):  # fmt: skip
                for m in re.finditer(pat, t, re.M):
                    found.add(name, m.group(1).rstrip(), sp.span_id, m.start(1),
                              m.start(1) + len(m.group(1).rstrip()))  # fmt: skip
            for pat, name in ((r"\(22\)[^:\n]*:?\s*", "filing_date"), (r"\(45\)[^:\n]*:?\s*",
                              "grant_date"), (r"\(43\)[^:\n]*:?\s*", "publication_date"),
                              (r"\bFil(?:ed|ing date)\s*:?\s*", "filing_date"),
                              (r"\bPublication date\s*:?\s*", "publication_date"),
                              (r"\bGranted?(?: on)?\s*:?\s*", "grant_date")):  # fmt: skip
                for m in re.finditer(pat, t):
                    dates = find_dates(t[m.end() : m.end() + 30], "dmy")
                    if dates and dates[0][1] == 0:
                        iso, s, e = dates[0]
                        found.add(name, iso, sp.span_id, m.end() + s, m.end() + e)
            for m in re.finditer(r"(?:\(30\)|\bPriority\b)[^:\n]*:\s*(.+)", t):
                for part in re.finditer(r"[A-Z][A-Z0-9-]*\d[\w/-]*[^;\n]*?(?=;|$|\n)",
                                        m.group(1)):  # fmt: skip
                    s0 = m.start(1) + part.start()
                    found.add("priority_claims", part.group(0).strip(), sp.span_id, s0,
                              s0 + len(part.group(0).strip()))  # fmt: skip
                    dates = find_dates(part.group(0), "dmy")
                    if dates and not found.has("priority_date"):
                        found.add("priority_date", dates[0][0], sp.span_id, s0 + dates[0][1],
                                  s0 + dates[0][2])  # fmt: skip
            for m in re.finditer(r"^\s*(Status\s*:\s*.+?)\s*$", t, re.M):
                found.add("status", m.group(1), sp.span_id, *m.span(1))
            for m in re.finditer(r"^\s*Legal status[^:\n]*:\s*(.+?)\s*$", t, re.M):
                found.add("status", m.group(1), sp.span_id, *m.span(1))
            stripped = t.strip()
            if re.fullmatch(r"(?:#+\s*)?(?:\*\*)?Claims(?:\*\*)?:?", stripped, re.I):
                in_claims, in_desc = True, False
                continue
            if re.fullmatch(r"(?:#+\s*)?(?:\*\*)?(?:Description|Detailed description)(?:\*\*)?:?",
                            stripped, re.I):  # fmt: skip
                in_desc, in_claims = True, False
                continue
            if re.fullmatch(r"(?:#+\s*)?(?:\*\*)?Abstract(?:\*\*)?:?", stripped, re.I):
                in_desc = in_claims = False
                found.items.setdefault("_abstract_next", [])
                continue
            if found.items.get("_abstract_next") is not None and not found.has("abstract") \
                    and stripped and not stripped.startswith("Page "):  # fmt: skip
                found.add("abstract", stripped, sp.span_id, t.find(stripped),
                          t.find(stripped) + len(stripped))  # fmt: skip
                found.items.pop("_abstract_next", None)
            if in_desc and stripped and not found.has("description") and \
                    not re.match(r"^Page \d", stripped):  # fmt: skip
                found.add("description", stripped, sp.span_id, t.find(stripped),
                          t.find(stripped) + len(stripped))  # fmt: skip
            if in_claims and re.match(r"^\s*(?:\d+[.)]|<claim)", t):
                found.add("claims", stripped, sp.span_id, t.find(stripped),
                          t.find(stripped) + len(stripped))  # fmt: skip
            if in_claims and re.match(r"^\s*(Legal status|Page \d)", t):
                in_claims = False
        found.items.pop("_abstract_next", None)


# --- tables and joins -----------------------------------------------------------------------

ROLE_WORDS: dict[str, tuple[str, ...]] = {
    "employer": ("employer", "organisation", "organization", "company", "affiliation", "org",
                 "institution", "firm"),
    "role": ("role", "title", "position", "job", "function"),
    "candidate": ("candidate", "invention", "project", "idea", "innovation", "disclosure for"),
    "contributed": ("contributed", "contribution", "contributions", "what they did", "work",
                    "did", "part"),
    "agreement_ref": ("agreement", "agreements", "ip", "terms", "contract", "assignment"),
    "source": ("source", "link", "file", "evidence", "record", "document", "doc", "where"),
    "event_type": ("type", "kind", "channel", "format", "venue", "occasion"),
    "what_was_shown": ("shown", "what", "summary", "description", "content", "material",
                       "disclosed", "showed"),
    "audience": ("audience", "attendees", "seen by", "recipients", "who saw"),
    "public": ("public", "is public", "publicly"),
    "by": ("by", "who", "presenter", "presenters", "people", "presented", "speaker"),
    "rank": ("rank", "priority", "#", "order"),
    "date": ("date", "on", "when", "day", "dated"),
}  # fmt: skip
_ID_VALUE = re.compile(r"^[A-Za-z]{1,8}(?:-[A-Za-z0-9]+)*-?\d+[A-Za-z0-9-]*$")


def _words(name: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return [w for w in re.split(r"[^a-z#]+", spaced.lower()) if w]


def guess_role(name: str, values: Sequence[str]) -> str:
    """The role of a column named ``name`` holding ``values`` (patent tables)."""
    words = _words(name)
    joined = " ".join(words)
    vals = [v for v in values if v]
    ids = bool(vals) and all(_ID_VALUE.match(v) for v in vals)
    if "id" in words or ids:
        if {"event", "disclosure", "ev"} & set(words):
            return "event_id"
        if {"person", "contributor", "inventor", "people", "member"} & set(words):
            return "person_id"
        if {"candidate", "invention", "project"} & set(words):
            return "candidate"
        if {"record", "row", "line"} & set(words) or words == ["id"]:
            return "record_id"
        if ids and {"event"} & set(words):
            return "event_id"
    if {"person", "contributor", "inventor", "member"} & set(words) and not ids:
        return "name"
    if (words and words[-1] == "name") or joined in ("name", "full name", "person name"):
        return "name"
    for role, names in ROLE_WORDS.items():
        if any(n == joined or n in words for n in names):
            if role == "date" and vals and not all(parse_date(v[:10], "dmy") or
                                                   find_dates(v, "dmy") for v in vals):  # fmt: skip
                continue
            return role
    if vals and all(parse_date(v[:10], "dmy") for v in vals):
        return "date"
    return "other"


def _columns(state: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in state.splitlines():
        if line.startswith("- "):
            name, _, values = line[2:].partition(": ")
            out[name] = [v.strip() for v in values.split(" | ") if v.strip()] if values else []
    return out


def _choice(spec: QuestionSpec, chosen: str) -> ClassAnswer:
    rest = (1 - SURE) / max(1, len(spec.options) - 1)
    return ClassAnswer(chosen, distribution_ppm({o: SURE if o == chosen else rest
                                                 for o in spec.options}))  # fmt: skip


class TableRoles(RuleClassificationModel):
    """``table_roles``: each column's role from its name's words, else its values."""

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """One role per column question."""
        cols = _columns(request.state)
        out = []
        for q in request.questions:
            m = re.search(r"column '(.*)' hold|column \"(.*)\" hold", q.prompt)
            name = (m.group(1) or m.group(2)) if m else ""
            role = guess_role(name, cols.get(name, []))
            out.append((q.name, _choice(q, role if role in q.options else "other")))
        return ClassificationResult(tuple(out))


STOP = frozenset(
    [
        "about",
        "after",
        "also",
        "from",
        "have",
        "into",
        "more",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "this",
        "those",
        "through",
        "using",
        "when",
        "where",
        "which",
        "while",
        "with",
        "would",
        "should",
        "could",
    ]
)


def stems(text: str) -> set[str]:
    """A text's distinctive words, cut to stems of five letters."""
    return {w.lower()[:5] for w in re.findall(r"[A-Za-z]{5,}", text) if w.lower() not in STOP}


class CandidateOf(RuleClassificationModel):
    """``candidate_of``: the candidate whose ID a text names, else whose title it shares most."""

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """The one ``candidate`` question."""
        text, _, rest = request.state.partition("\ncandidates:\n")
        text = text.removeprefix("text: ")
        titles = {}
        for line in rest.splitlines():
            cid, _, title = line[2:].partition(": ")
            titles[cid] = title
        out = []
        for q in request.questions:
            named = [c for c in titles if re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", text)]
            if len(named) == 1:
                out.append((q.name, _choice(q, named[0])))
                continue
            words = stems(text)
            scored = sorted(((len(words & stems(t)), c) for c, t in titles.items()), reverse=True)
            best = scored[0] if scored else (0, "none")
            tie = len(scored) > 1 and scored[1][0] == best[0]
            alone = best[0] >= 1 and all(n == 0 for n, _ in scored[1:])  # the only one it names
            pick = best[1] if (best[0] >= 2 or alone) and not tie else "none"
            out.append((q.name, _choice(q, pick if pick in q.options else "none")))
        return ClassificationResult(tuple(out))


# --- the decide step ----------------------------------------------------------------------

MEETING = re.compile(r"\b(minutes|attendees|present:|agenda|action items?|meeting|call notes|"
                     r"workshop|stand-?up|retro)\b", re.I)  # fmt: skip
EVENT = re.compile(r"\b(demo|demonstrat\w*|trade show|expo|conference|talk|webinar|field day|"
                   r"open day|poster|blog post|press release|pilot|shown|showed|audience|"
                   r"disclosure log|posted|video)\b", re.I)  # fmt: skip
PUBLICATION = re.compile(r"\(1[19]\)|\(2[12]\)|\(5[147]\)|\bpublication (?:number|no)\b|"
                         r"\bkind code\b", re.I)  # fmt: skip
MECHANISM = re.compile(r"\b(" + "|".join(re.escape(w) for w in HEADINGS["mechanism"]) + r")\b",
                       re.I)  # fmt: skip
STEPS = re.compile(r"(?:^|\s)(?:\d{1,2}[.)]|step \d+[:.]?)\s+[A-Z]")


def _preview(state: str) -> dict[str, Any]:
    out: dict[str, Any] = {"columns": {}}
    for line in state.splitlines():
        if line.startswith("- ") and ": " in line:
            name, _, values = line[2:].partition(": ")
            out["columns"][name] = [v.strip() for v in values.split(" | ") if v.strip()]
        elif ": " in line:
            k, _, v = line.partition(": ")
            out.setdefault(k, v)
    return out


def decide_record(state: str, options: Sequence[str]) -> tuple[str, Decimal]:
    """The kind a patent paralegal gives a record from its structural preview, and how sure.

    A table: people beside what they contributed is a contributor record; dates beside
    what was shown is a disclosure record; a publication number among its fields is a
    publication. A text: front-page codes or a publication number is a publication; a
    blank form names itself a form and lists fields; ranked priorities name themselves;
    an email has email headers; a meeting's notes say so; a write-up of a demo or a talk
    is a disclosure record; a text with a mechanism (a method heading, numbered steps) is
    an invention disclosure. Two at once (an email describing a mechanism) is decided at
    0.6, inside the abstention band, and so read as ``other``.
    """
    got = _preview(state)
    fmt = got.get("format", "text")
    fields = got.get("header fields", "").lower()
    cols: dict[str, list[str]] = got["columns"]
    if fmt in ("csv", "json"):
        if re.search(r"\b(publication|pub|patent|doc|document)[_ ]?(number|no|num)\b|"
                     r"\bkind_code\b", fields) and re.search(r"\b(abstract|title|claims?)\b",
                                                           fields + " " + " ".join(cols)):  # fmt: skip  # noqa: E501
            return "prior_art_publication", SURE
        roles = {guess_role(c, v) for c, v in cols.items()}
        if ({"person_id", "name"} & roles) and "contributed" in roles:
            return "contributor_record", SURE
        if "date" in roles and "what_was_shown" in roles:
            return "disclosure_record", SURE
        if "rank" in roles and re.search(r"\b(goal|priorit|objective|aim|focus)", " ".join(cols),
                                         re.I):  # fmt: skip
            return "business_priorities", SURE
        return "other", SURE
    text = got.get("text", "")
    heads = got.get("headings", "")
    lead = heads + " " + text[:400]
    found: list[str] = []
    if PUBLICATION.search(text[:1500]) or (re.search(r"\bclaims\b", heads, re.I)
                                           and re.search(r"\babstract\b", text, re.I)):  # fmt: skip
        return "prior_art_publication", SURE
    if FORM_TITLE.search(lead) and int(got.get("table rows", "0") or 0) >= 3:
        return "form_template", SURE
    if FORM_TITLE.search(heads) and re.search(r"_{3,}|\[ \]|:\s*$", text):
        return "form_template", SURE
    if PRIORITY_WORDS.search(heads) and re.search(r"\brank|\b1[.)]\s", text, re.I):
        return "business_priorities", SURE
    mechanism = bool(MECHANISM.search(heads)) or len(STEPS.findall(text)) >= 2
    if fmt == "email":
        return "email", SURE
    if (MEETING.search(heads) or re.search(r"\b(attendees|present|participants)\s*:", text, re.I)
            or re.search(r"^\W*(?:\w+\s+){0,3}(?:met with|meeting with|call with)\b", text,
                         re.I)):  # fmt: skip
        found.append("meeting_notes")
    elif EVENT.search(heads + " " + text[:160]) and not mechanism:
        found.append("disclosure_record")
    if mechanism and "meeting_notes" not in found:
        found.append("invention_disclosure")
    found = [k for k in dict.fromkeys(found) if k in options]
    if not found:
        return "other", SURE
    if len(found) == 1:
        return found[0], SURE
    return found[0], Decimal("0.6")


class DocKind(RuleClassificationModel):
    """``doc_kind``: the decide step, by :func:`decide_record` over the preview."""

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """The one ``kind`` question."""
        out = []
        for q in request.questions:
            kind, sure = decide_record(request.state, q.options)
            kind = kind if kind in q.options else "other"
            rest = (1 - sure) / max(1, len(q.options) - 1)
            weights = {o: sure if o == kind else rest for o in q.options}
            out.append((q.name, ClassAnswer(kind, distribution_ppm(weights))))
        return ClassificationResult(tuple(out))
