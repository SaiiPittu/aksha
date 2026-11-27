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

from .world import quantities

SURE = Decimal("0.95")
ABSENT = ppm(Decimal("0.9"))

HEADINGS: dict[str, tuple[str, ...]] = {
    "problem": ("problem", "background", "need", "motivation", "context", "pain", "why"),
    "mechanism": ("mechanism", "method", "how it works", "how it would work", "how it will work",
                  "how we do it", "steps", "process", "proposal",
                  "procedure", "detailed description", "description of the invention",
                  "embodiment", "approach", "the idea", "solution", "algorithm", "design",
                  "technique", "proposed method", "sequence", "operation", "how it runs",
                  "what we drew", "method as"),
    "parameters": ("parameters", "settings", "values", "constants", "configuration",
                   "key numbers", "tuning", "numbers"),
    "alternatives": ("alternatives", "options", "considered", "rejected", "what we tried",
                     "other approaches", "discarded", "dropped", "ruled out", "tried instead",
                     "tried first", "instead"),
    "evidence": ("evidence", "status", "testing", "test results", "validation", "results",
                 "where it stands", "maturity", "build status", "trials"),
    "open": ("open", "unresolved", "questions", "to do", "todo", "next steps", "gaps",
             "action items", "follow-ups", "follow ups", "where the numbers will come from"),
}  # fmt: skip
"""What a section's heading says, by the role of the section."""

_MD = re.compile(r"^\s*(#{1,6})\s+(.*?)\s*#*\s*$")
_BOLD = re.compile(r"^\s*(?:\*\*|__)(.{2,80}?)(?:\*\*|__)\s*:?\s*$")
_SLIDE = re.compile(r"^\s*(?:slide|page)\s+\d+\s*[:.\u2013\u2014-]\s*(.+?)\s*$", re.I)
_COLON = re.compile(r"^\s*([A-Z][^.!?:|]{1,60}):\s*$")
_ITEM = re.compile(r"^\s*([-*+•▪◦]|\d{1,3}[.)]|[A-Z]{1,3}\d{1,3}[.)]|[a-z][.)]|\(\w{1,3}\))\s+")
_CAPS = re.compile(r"^\s*([A-Z][A-Z0-9,&/' -]{2,60}[A-Z0-9])\s*$")
_KV = re.compile(r"^\s*([A-Za-z][A-Za-z _-]{0,30}?)\.*:[ \t]*(.+?)\s*$")


@dataclass(frozen=True)
class Line:
    """One passage, parsed: what it is, its body's offsets inside the passage."""

    span: str
    text: str
    kind: str  # heading, item, row, meta, paragraph
    level: int
    body: tuple[int, int]
    marker: str = ""


_JSON_FIELD = re.compile(r'^\s*"([^"]{1,60})"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,?\s*$')
_JSON_OPEN = re.compile(r'^\s*"([^"]{1,60})"\s*:\s*[\[{]\s*$')
_JSON_STRING = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*,?\s*$')
_XML_LEAF = re.compile(r"^\s*<([A-Za-z_][\w.-]*)((?:\s+[\w.:-]+=\"[^\"]*\")*)\s*>(.*?)</\1>\s*$")
_XML_OPEN = re.compile(r"^\s*<([A-Za-z_][\w.-]*)((?:\s+[\w.:-]+=\"[^\"]*\")+)\s*>\s*$")
_XML_ATTR = re.compile(r'([\w.:-]+)="([^"]*)"')
_TURN = re.compile(
    r"^\s*(?:<v(?:\.[\w.]+)?\s+([^>]+)>|\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s+([^:\[\]]{1,40}?):\s)"
)
_RULE_LINE = re.compile(r"^\s*[-=_*~]{3,}.*[-=_*~]{3,}\s*$|^\s*[-=_*~]{3,}\s*$")
_STEP_LABEL = re.compile(r"^([A-Z]{1,3}\d{1,3})(?::|\.|\s{2,}|\s(?=[A-Z]))\s*")
"""A step's own label inside its text: ``A2: call it...``, ``G1  Flatness``."""
STEP_TAGS = frozenset({"step", "steps", "item", "li"})


def parse_line(span: str, text: str) -> Line:
    """What a passage is, by its marks, and where its body sits.

    Besides text marks it reads the line shapes of structured exports: a JSON field
    (``"key": "value",``), a key opening a JSON list or object (a heading), a bare JSON
    string in a list (an item), an XML element holding text (a field, or an item when it is
    a step), an XML element opening a record (a heading whose attributes are fields), a
    speaker's turn of a transcript (``turn``) and a rule line (``rule``).
    """
    stripped = text.strip()
    lead = len(text) - len(text.lstrip())
    if "\n" not in stripped:
        m = _JSON_FIELD.match(text)
        if m:
            return Line(span, text, "meta", 0, m.span(2), m.group(1).strip().lower())
        m = _JSON_OPEN.match(text)
        if m:
            return Line(span, text, "heading", 3, m.span(1))
        m = _JSON_STRING.match(text)
        if m and len(m.group(1)) > 3:
            label = _STEP_LABEL.match(m.group(1))
            return Line(span, text, "item", 0, m.span(1), label.group(1) if label else "")
        m = _XML_LEAF.match(text)
        if m:
            tag = m.group(1).lower()
            attrs = dict(_XML_ATTR.findall(m.group(2)))
            kind = "item" if tag in STEP_TAGS else "meta"
            marker = str(attrs.get("n", attrs.get("id", ""))) if kind == "item" else tag
            return Line(span, text, kind, 0, m.span(3), marker)
        m = _XML_OPEN.match(text)
        if m:
            return Line(span, text, "heading", 2, m.span(2), m.group(1).lower())
        if _RULE_LINE.match(text):
            return Line(span, text, "rule", 0, (lead, len(text.rstrip())))
    m = re.match(r"^\s*<([A-Za-z_][\w.-]*)(?:\s[^<>]*)?>\s*(?=\S)", text)
    if m and "\n" in stripped and f"</{m.group(1)}>" not in text:  # an element's text, wrapped
        return Line(span, text, "meta", 0, (m.end(), len(text.rstrip())), m.group(1).lower())
    m = _TURN.match(text)
    if m:
        return Line(span, text, "turn", 0, (m.end(), len(text.rstrip())),
                    (m.group(1) or m.group(2) or "").strip())  # fmt: skip
    for pattern, level in ((_MD, 0), (_SLIDE, 2), (_BOLD, 3), (_COLON, 3)):
        m = pattern.match(text)
        if m:
            g = m.lastindex or 1
            lv = len(m.group(1)) if pattern is _MD else level
            body = m.group(2) if pattern is _MD else m.group(g)
            at = text.find(body, m.start(g))
            return Line(span, text, "heading", lv, (at, at + len(body)))
    m = _CAPS.match(text)
    if m and "\n" not in stripped and len(re.findall(r"[A-Z]{2,}", stripped)) >= 1 \
            and not _STEP_LABEL.match(stripped):  # fmt: skip
        return Line(span, text, "heading", 2, m.span(1))
    if stripped.startswith("|"):
        return Line(span, text, "row", 0, (lead, len(text.rstrip())))
    m = _ITEM.match(text)
    if m and "\n" not in stripped[:2]:
        return Line(span, text, "item", 0, (m.end(), len(text.rstrip())), m.group(1))
    m = _KV.match(text)
    if m and "\n" not in text and len(stripped) < 160:
        return Line(span, text, "meta", 0, (m.start(2), m.end(2)), m.group(1).strip().lower())
    return Line(span, text, "paragraph", 0, (lead, len(text.rstrip())))


def _lines_of(span: str, text: str) -> list[Line]:
    """A passage as lines: a JSON record line by line (``"key": "value",``), else whole.

    Each line's offsets stay offsets of the passage, so an answer cites the passage.
    """
    if not text.lstrip().startswith(("{", "[")) or "\n" not in text:
        return [parse_line(span, text)]
    out = []
    at = 0
    for part in text.split("\n"):
        if part.strip():
            ln = parse_line(span, part)
            pad = " " * at  # the line's text at its place in the passage: offsets carry over
            out.append(Line(span, pad + part, ln.kind, ln.level,
                            (ln.body[0] + at, ln.body[1] + at), ln.marker))  # fmt: skip
        at += len(part) + 1
    return out


def _titles_after_rules(lines: list[Line]) -> list[Line]:
    """A short line right after a page's rule is that page's title: a heading."""
    out = list(lines)
    for i in range(1, len(out)):
        ln = out[i]
        body = ln.text.strip()
        if out[i - 1].kind == "rule" and ln.kind == "paragraph" and "\n" not in body \
                and len(body) <= 60 and body[-1:] not in ".!?":  # fmt: skip
            lead = len(ln.text) - len(ln.text.lstrip())
            out[i] = Line(ln.span, ln.text, "heading", 2, (lead, lead + len(body)))
    return out


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
                                    r"field trial|tested on|test run|shipping|in production|"
                                    r"live since|built into)\b", re.I)),
    ("in_use_under_nda", re.compile(r"\b(pilot|deployed|in use|running at)\b", re.I)),
    ("prototype", re.compile(r"\b(prototype|proof of concept)\b", re.I)),
)  # fmt: skip
"""How far an invention is built, by the words its evidence uses (first match wins)."""
_REF = re.compile(r"`([^`]+\.[A-Za-z]{2,7})`|\b([\w-]+\.(?:csv|tsv|xlsx|xls|ods|pdf|json|txt|docx|"
                  r"pptx|parquet|zip|h5|mat|npz|log))\b")  # fmt: skip


_ORDINAL = {"first": 1, "one": 1, "second": 2, "two": 2, "third": 3, "three": 3,
            "fourth": 4, "four": 4, "fifth": 5, "five": 5, "sixth": 6, "six": 6}  # fmt: skip
_STEP_WORD = re.compile(r"\b(?:(first|second|third|fourth|fifth|sixth)\s+step|step\s+(\d{1,2}|one|"
                        r"two|three|four|five|six))\b", re.I)  # fmt: skip
_WE_ACT = re.compile(r"\bwe\s+(?:only\s+|then\s+|also\s+|just\s+|always\s+)?(?!can\b|could\b|"
                     r"should\b|tried\b|dropped\b|did\b|have\b|are\b|were\b|need\b|want\b|"
                     r"agreed\b|take\b|still\b|see\b|think\b)[a-z]{3,}", re.I)  # fmt: skip
_QUANT = re.compile(r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|twelve|half)\b", re.I)
_REJECT = re.compile(r"\b(dropped|ruled out|abandoned|gave up|scrapped|rejected)\b", re.I)
_OPEN = re.compile(r"\b(nobody (?:has )?(?:written|recorded|knows|decided|wrote)|not (?:yet )?"
                   r"(?:written down|decided|known|settled)|open question|stay open|leave it open|"
                   r"to be (?:decided|settled|confirmed)|tbd)\b", re.I)  # fmt: skip
_LABELLED = re.compile(r"(?m)^[ \t]*([A-Z]{1,3}\d{1,3})(?:[.:)]|[ \t]{2,})[ \t]*")


_WHY_DROPPED = re.compile(r"(dropped|rejected|ruled out|abandoned)\b", re.I)


def sentences(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """The sentences of ``text[start:end]``: cut at a stop before a capital, never inside 0.05."""
    out = []
    at = start
    for m in re.finditer(r"[.!?](?=\s+[A-Z\"'(\[]|\s*$)", text[start:end]):
        cut = start + m.end()
        out.append((at, cut))
        at = cut
    if at < end:
        out.append((at, end))
    trimmed = []
    for s, e in out:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            trimmed.append((s, e))
    return trimmed


_OPINION = re.compile(r"\b(I|we) (think|believe|feel)\b.{0,80}\b(strong\w*|best|novel|new|unique|"
                      r"nobody|no one)\b", re.I)  # fmt: skip
"""A person's view of how strong or new the idea is."""
_LEAD_ID = re.compile(r"(?:[A-Z][A-Z0-9]{0,7}(?:-[A-Z][A-Z0-9]{0,7})*-\d{1,6}[\s,:;.-]*)?")
"""An identifier leading a title line (``Project: XX-12, the title``), with its separator."""
TITLE_ORDER = ("title", "summary", "subject", "re", "topic", "project")
TITLE_KEYS = frozenset(TITLE_ORDER)
DATE_KEYS = frozenset({"date", "dated", "meeting date", "recorded", "when", "written", "created",
                       "captured"})  # fmt: skip


class NoteStructure(RuleExtractionModel):
    """``note_structure``: an invention note in any layout (module doc)."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """Every field the request names."""
        found = _Found()
        lines = [ln for s in request.state for ln in _lines_of(s.span_id, s.text)]
        lines = _titles_after_rules(lines)
        sections = _sections(lines)
        for role, heading, body in sections:
            if role is None:
                self._fields(body, found)
                continue
            items = [ln for ln in body if ln.kind in ("item", "row")]
            if role == "mechanism":
                steps = [ln for ln in body if ln.kind == "item"]
                if steps:
                    for ln in steps:
                        found.add("mechanism_steps", ln.text[ln.body[0] : ln.body[1]], ln.span,
                                  *ln.body)  # fmt: skip
                    for ln in body:  # a later addition written beside the list: (b2) ...
                        m = re.search(r"\((\w{1,3})\)\s+(?=\S)", ln.text[ln.body[0] : ln.body[1]])
                        if ln.kind == "paragraph" and m:
                            found.add("mechanism_steps", ln.text[ln.body[0] + m.end() : ln.body[1]],
                                      ln.span, ln.body[0] + m.end(), ln.body[1])  # fmt: skip
                elif not self._labelled(body, found):
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
                    s0, e0 = ln.body
                    if field == "alternatives":  # the alternative itself, not why it was dropped
                        parts = sentences(ln.text, s0, e0)
                        why = next((i for i, (a, _) in enumerate(parts)
                                    if i and _WHY_DROPPED.match(ln.text[a:])), None)  # fmt: skip
                        if why is not None:
                            e0 = parts[why - 1][1]
                    found.add(field, ln.text[s0:e0], ln.span, s0, e0)
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
                if not items:  # aligned lines: "Patch size ...... 12 mm", "Cap   | 8 mA"
                    for ln in body:
                        if ln.kind in ("paragraph", "meta"):
                            self._value_lines(ln, found)
            elif role in ("problem", "evidence"):
                first = next((ln for ln in body if ln.kind in ("paragraph", "item", "meta")),
                             None)  # fmt: skip
                if first is not None and not found.has(role):
                    found.add(role, first.text[first.body[0] : first.body[1]], first.span,
                              *first.body)  # fmt: skip
                if role == "evidence":
                    hay = [heading, *body] if heading is not None else list(body)
                    self._status(hay, found)
        if not found.has("mechanism_steps"):
            xml_steps = [ln for ln in lines if ln.kind == "item" and ln.text.lstrip()[:1] == "<"]
            for ln in xml_steps:
                found.add("mechanism_steps", ln.text[ln.body[0] : ln.body[1]], ln.span, *ln.body)
        if not found.has("mechanism_steps"):
            self._labelled(lines, found)
        if not found.has("mechanism_steps"):  # an enumerated list under no section: the steps
            loose = [
                ln for role, _, body in sections if role is None for ln in body
                if ln.kind == "item" and re.fullmatch(r"\(?[a-z0-9]{1,2}[.)]?", ln.marker)
                and len(ln.text[ln.body[0] : ln.body[1]].split("\n")[0].split()) >= 5
            ]  # fmt: skip
            if len(loose) >= 2:
                for ln in loose:
                    found.add("mechanism_steps", ln.text[ln.body[0] : ln.body[1]], ln.span,
                              *ln.body)  # fmt: skip
        if not found.has("mechanism_steps") and sum(ln.kind == "turn" for ln in lines) >= 3:
            self._spoken(lines, found)
        if not any(role for role, _, _ in sections) and not found.has("mechanism_steps"):
            self._prose(lines, found)
        for ln in lines:  # a person's view of the idea: a comment field, or "I think ..."
            said = ln.text[ln.body[0] : ln.body[1]]
            m = _OPINION.search(said) if ln.kind in ("paragraph", "turn", "meta") else None
            if m is None:
                continue
            s0 = ln.body[0] + max(said.rfind(".", 0, m.start()) + 1, 0)
            while ln.text[s0].isspace():
                s0 += 1
            end = said.find(".", m.end())
            e0 = ln.body[0] + (end + 1 if end >= 0 else len(said))
            found.add("opinions", ln.text[s0:e0], ln.span, s0, e0)
        if found.has("mechanism_steps") and not found.has("parameters"):
            for value, ref in list(found.items.get("mechanism_steps", [])):
                for a, b in quantities(value):  # the values a note states inside its steps
                    found.add("parameters", value[a:b], ref.span_id, ref.start + a, ref.start + b)
        if found.has("mechanism_steps"):
            for ln in lines:  # a field left blank ("Witnessed: ____") is left open by the record
                blank = re.search(r"\b[A-Z]\w*(?: \w+){0,3}:\s*_{3,}.*$", ln.text)
                if blank:
                    e = len(ln.text[: blank.end()].rstrip())
                    found.add("open_points", ln.text[blank.start() : e], ln.span, blank.start(), e)
            mech = next((h for r, h, _ in sections if r == "mechanism" and h is not None), None)
            if mech is not None and not found.has("implementation_status"):
                self._status([mech], found)
            if not found.has("implementation_status"):
                self._status(list(lines), found)
            self._identity(lines, found)
            for ln in lines:
                for m in _REF.finditer(ln.text):
                    g = 1 if m.group(1) else 2
                    found.add("references", m.group(g), ln.span, m.start(g), m.end(g))
        return found.result(request)

    def _fields(self, body: Sequence[Line], found: _Found) -> None:
        """Fields outside any section, by their keys' roles (``"context": "..."``)."""
        for ln in body:
            if ln.kind != "meta":
                continue
            role = role_of(ln.marker)
            field = {"problem": "problem", "evidence": "evidence", "open": "open_points",
                     "alternatives": "alternatives"}.get(role or "")  # fmt: skip
            if field and not (field in ("problem", "evidence") and found.has(field)):
                found.add(field, ln.text[ln.body[0] : ln.body[1]], ln.span, *ln.body)
            if role == "evidence":
                self._status([ln], found)

    def _value_lines(self, ln: Line, found: _Found) -> None:
        """Each physical line of ``ln`` that names a value: one parameter."""
        at = ln.body[0]
        for part in ln.text[ln.body[0] : ln.body[1]].split("\n"):
            s = at + len(part) - len(part.lstrip())
            e = at + len(part.rstrip())
            body = ln.text[s:e]
            if re.search(r"\d", body) and re.match(r"[A-Za-z]", body) and len(body) < 120:
                found.add("parameters", body, ln.span, s, e)
            at += len(part) + 1

    def _labelled(self, lines: Sequence[Line], found: _Found) -> bool:
        """Steps written as labelled blocks (``G1  Flatness. ...``): each label one step."""
        got = False
        for ln in lines:
            if ln.kind not in ("paragraph", "item"):
                continue
            chunk = ln.text[ln.body[0] : ln.body[1]]
            marks = list(_LABELLED.finditer(chunk))
            if len(marks) < 2:
                continue
            for m, nxt in zip(marks, [*marks[1:], None], strict=True):
                s = ln.body[0] + m.end()
                e = ln.body[0] + (nxt.start() if nxt else len(chunk))
                while e > s and ln.text[e - 1].isspace():
                    e -= 1
                found.add("mechanism_steps", ln.text[s:e], ln.span, s, e)
                got = True
        return got

    def _spoken(self, lines: Sequence[Line], found: _Found) -> None:
        """A procedure said aloud in a meeting or a call, read from its speakers' turns.

        A step is a sentence naming a step (``the first step``, ``step four``: the step is
        what follows its colon, or the turn's next sentence), or a sentence where the
        speakers say what they do with a quantity (``we only accept a 30-second window
        when ...``). A sentence saying something was dropped is an alternative, with the
        sentence before it; one saying nobody has written something down is an open
        point; a sentence listing three or more named values gives the parameters.
        """
        steps: list[tuple[int | None, str, int, int]] = []
        for ln in lines:
            if ln.kind != "turn":
                continue
            sents = sentences(ln.text, *ln.body)
            skip: set[int] = set()
            for i, (s, e) in enumerate(sents):
                if i in skip:
                    continue
                sent = ln.text[s:e]
                named = _STEP_WORD.search(sent)
                if named:
                    word = (named.group(1) or named.group(2) or "").lower()
                    n = int(word) if word.isdigit() else _ORDINAL.get(word)
                    colon = sent.find(":", named.end())
                    if colon > 0 and len(sent[colon + 1 :].split()) >= 4:
                        steps.append((n, ln.span, s + colon + 1, e))
                    elif i + 1 < len(sents):
                        ns, ne = sents[i + 1]
                        nxt = ln.text[ns:ne]
                        colon = nxt.find(":")
                        if colon > 0 and _WE_ACT.search(nxt[colon:]):
                            ns += colon + 1
                        steps.append((n, ln.span, ns, ne))
                        skip.add(i + 1)
                    continue
                if _REJECT.search(sent):
                    a = sents[i - 1][0] if i and len(ln.text[slice(*sents[i - 1])].split()) > 3 \
                        else s  # fmt: skip
                    found.add("alternatives", ln.text[a:e], ln.span, a, e)
                    continue
                opened = _OPEN.search(sent)
                if opened and not sent.rstrip().endswith("?") and len(sent.split()) >= 6:
                    colon = sent.find(":")
                    a = s + colon + 1 if 0 < colon < opened.start() else s
                    found.add("open_points", ln.text[a:e], ln.span, a, e)
                    continue
                body = sent.split(":", 1)[-1]
                chunks = [c for c in re.split(r",\s+|;\s+|\s+and\s+(?=\S+\s+\S+\s+\d)", body)
                          if c.strip()]  # fmt: skip
                if len(chunks) >= 3 and sum(bool(re.search(r"\d", c)) for c in chunks) >= 3:
                    at = s + (sent.find(":") + 1 if ":" in sent else 0)
                    for c in re.finditer(r"[^,;]+", ln.text[at:e]):
                        cs, ce = at + c.start(), at + c.end()
                        while cs < ce and ln.text[cs].isspace():
                            cs += 1
                        while ce > cs and ln.text[ce - 1] in " .":
                            ce -= 1
                        if re.search(r"\d", ln.text[cs:ce]):
                            found.add("parameters", ln.text[cs:ce], ln.span, cs, ce)
                    continue
                if sent.rstrip().endswith("?"):
                    continue
                if _WE_ACT.search(sent) and _QUANT.search(sent):
                    colon = sent.find(":")
                    if colon > 0 and _WE_ACT.search(sent[colon:]):
                        s += colon + 1
                        while ln.text[s].isspace():
                            s += 1
                    steps.append((None, ln.span, s, e))
        if not steps:
            return
        taken: set[int] = {n for n, *_ in steps if n is not None}
        free = 1
        ordered: list[tuple[int, str, int, int]] = []
        for num, span, s, e in steps:
            if num is None:
                while free in taken:
                    free += 1
                num = free
                taken.add(num)
            ordered.append((num, span, s, e))
        for _n, span, s, e in sorted(ordered, key=lambda x: x[0]):
            text = next(ln.text for ln in lines if ln.span == span)
            if text[e - 1 : e] == ".":
                e -= 1
            found.add("mechanism_steps", text[s:e], span, s, e)

    def _prose(self, lines: Sequence[Line], found: _Found) -> None:
        """A plain note with no sections, read as an invention note.

        Each sentence saying how a thing works is a step, the thing it names the title, and
        its words how far it is built. Only a note that says someone made or thought of the
        thing (built, designed, an idea) is read so.
        """
        if not any(_MADE.search(ln.text) for ln in lines):
            return
        how = [ln for ln in lines if ln.kind in ("paragraph", "item") and _HOW.search(ln.text)]
        if len(how) < 2 and sum(len(ln.text) for ln in lines) > 800:
            return  # one "using" in a long document (an agreement) describes no invention
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
            elif key in TITLE_KEYS and not found.has("title"):
                lead = _LEAD_ID.match(clean)
                ts = lead.end() if lead else 0
                if ts and not found.has("candidate_id"):
                    tok = clean[:ts].rstrip(" ,:;.-")
                    found.add("candidate_id", tok, span, cs, cs + len(tok))
                if clean[ts:].strip():
                    found.add("title", clean[ts:], span, cs + ts, cs + len(clean))
            elif key in DATE_KEYS:
                dates = find_dates(clean, "dmy") or find_dates(clean, "mdy")
                if dates:
                    iso, ds, de = dates[0]
                    dated = (iso, span, cs + ds, cs + de)  # the last dated line wins
        best_title: tuple[Any, ...] | None = None
        for ln in lines:  # an export's own fields: <summary>, "key": ..., "created": ...
            if ln.kind != "meta" or not ln.marker:
                continue
            body = ln.text[ln.body[0] : ln.body[1]]
            if ln.marker in TITLE_KEYS and body.strip():
                rank = TITLE_ORDER.index(ln.marker)
                if best_title is None or rank < best_title[0]:
                    best_title = (rank, body, ln.span, *ln.body)
            elif ln.marker in ("key", "id", "idea", "ref") and _ID_VALUE.match(body.strip()) \
                    and not found.has("candidate_id"):  # fmt: skip
                found.add("candidate_id", body.strip(), ln.span, *ln.body)
            elif ln.marker in DATE_KEYS and dated is None:
                dates = find_dates(body, "dmy") or find_dates(body, "mdy")
                if dates:
                    iso, ds, de = dates[0]
                    dated = (iso, ln.span, ln.body[0] + ds, ln.body[0] + de)
        if best_title is not None and not found.has("title"):
            found.add("title", *best_title[1:])
        for ln in lines:  # a record's opening element: <idea key="X-3" created="...">
            if ln.kind != "heading" or not ln.text.lstrip().startswith("<"):
                continue
            for m in re.finditer(r'([\w.-]+)="([^"]*)"', ln.text):
                k, v = m.group(1).lower(), m.group(2)
                if k in ("key", "id", "ref") and _ID_VALUE.match(v) and not found.has(
                        "candidate_id"):  # fmt: skip
                    found.add("candidate_id", v, ln.span, *m.span(2))
                elif k in DATE_KEYS and dated is None and find_dates(v, "dmy"):
                    iso, ds, de = find_dates(v, "dmy")[0]
                    dated = (iso, ln.span, m.start(2) + ds, m.start(2) + de)
            break
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
        if self._ranked(lines, found):
            return found.result(request)
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
                                                                        "order", "no", "level")),
                               None)  # fmt: skip
                rank = cells[rank_at] if rank_at is not None and rank_at < len(cells) else ""
                digits = re.fullmatch(r"[A-Za-z]{0,2}\s?(\d{1,2})", rank)
                n = int(digits.group(1)) if digits else len(rows) + 1
                rows.append((n, ln, ln.body))
        for _, ln, (s, e) in sorted(rows, key=lambda r: r[0]):
            found.add("items", ln.text[s:e], ln.span, s, e)
        return found.result(request)

    def _ranked(self, lines: Sequence[Line], found: _Found) -> bool:
        """Items that carry their rank in their own label, in rank order.

        Sub-headings labelled with a rank (``O1 (top priority): ...``, ``P0 ...``), else lines
        that lead with one (``Priority 1: ...``). The label, not the order written, ranks.
        """
        label = re.compile(r"^\s*(?:priority|rank|level|[A-Z]{1,2})\s?(\d{1,2})\b(?![.\d])", re.I)
        heads = [(int(m.group(1)), ln) for ln in lines if ln.kind == "heading"
                 for m in [label.match(ln.text[ln.body[0] : ln.body[1]])] if m]  # fmt: skip
        said: list[tuple[int, Line, int, int]] = []
        for ln in lines:
            if ln.kind not in ("meta", "paragraph", "item"):
                continue
            at = 0
            for part in ln.text.split("\n"):  # each physical line of a joined paragraph
                m = label.match(part)
                if m:
                    s0 = at + len(part) - len(part.lstrip())
                    said.append((int(m.group(1)), ln, s0, at + len(part.rstrip())))
                at += len(part) + 1
        if len({n for n, _ in heads}) >= 2:
            for _, ln in sorted(heads, key=lambda r: r[0]):
                found.add("items", ln.text[ln.body[0] : ln.body[1]], ln.span, *ln.body)
            return True
        if len({n for n, *_ in said}) >= 2:
            for _, ln, s0, e0 in sorted(said, key=lambda r: r[0]):
                found.add("items", ln.text[s0:e0], ln.span, s0, e0)
            return True
        return False

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


_KEY_WORDS = re.compile(r"[a-z0-9]+")
_NUMBER_WORDS = frozenset({"no", "nr", "num", "number"})
_DOCUMENT_WORDS = frozenset({"pub", "publication", "patent", "doc", "document"})
_PRIORITY_WITH = frozenset({"claim", "claims", "date", "no", "nr", "number", "application", "data"})


def biblio_field(key: str) -> str | None:
    """What a bibliographic key names, read from the words the key is made of.

    The key is split into words (``pubNo``, ``pub-kind`` and ``Legal status`` alike) and read
    by the vocabulary of publication data in general (WIPO ST.9 and ST.16 terms and their
    usual abbreviations), in a fixed order: a kind code, a priority claim, an event's date, a
    status, an office, a title, an abstract, a description, claims, a link and, last, the
    publication's number (a number word with a document word, or ``number`` alone). No
    export's own spelling is listed: a key is read by what its words mean.
    """
    words = _KEY_WORDS.findall(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(key)).lower())
    has = set(words)
    if not has:
        return None

    def stem(*stems: str) -> bool:
        return any(w.startswith(s) for w in words for s in stems)

    if "kind" in has:
        return "kind_code"
    if stem("priorit") and (len(words) == 1 or has & _PRIORITY_WITH):
        return "priority_claims"
    if stem("filed", "filing") or {"application", "date"} <= has:
        return "filing_date"
    if stem("grant"):
        return "grant_date"
    if stem("published") or {"publication", "date"} <= has:
        return "publication_date"
    if "status" in has:
        return "status"
    if has & {"office", "authority"}:
        return "office"
    if "title" in has:
        return "title"
    if has & {"abstract", "summary"}:
        return "abstract"
    if "description" in has:
        return "description"
    if stem("claim"):
        return "claims"
    if has & {"url", "uri", "link", "href", "web", "website"}:
        return "url"
    if has & _NUMBER_WORDS and len(words) > 1 and has & _DOCUMENT_WORDS:
        return "number"
    if len(words) == 1 and has & {"pub", "publication", "patent", "number"}:
        return "number"
    return None


_NUMBER_KIND = re.compile(r"(?<![\w-])([A-Z]{2,}(?:-[A-Z0-9]+)*-\d[\w-]*)\s+([A-Z]\d?)\b(?!-)")
"""A publication number followed by its kind code (WIPO ST.16): ``XX-1234 B1``."""
EVENT_DATES = (("priorit", "priority_date"), ("fil", "filing_date"), ("grant", "grant_date"),
               ("publish", "publication_date"))  # fmt: skip


def _loads(text: str) -> Any:
    try:
        return json.loads(text.strip().rstrip(","))
    except ValueError:
        return None


def _leaves(data: Any, path: str = "", parent: Any = None) -> Iterator[tuple[str, Any, Any]]:
    """Every scalar of a JSON tree in document order: its dotted path, value and container."""
    if isinstance(data, dict):
        for k, v in data.items():
            yield from _leaves(v, f"{path}.{k}" if path else str(k).lower(), data)
    elif isinstance(data, list):
        for v in data:
            yield from _leaves(v, path, data if not isinstance(v, (dict, list)) else parent)
    elif isinstance(data, str):
        yield path, data, parent


class _Only:
    """A :class:`_Found` that takes no more answers for the fields already answered."""

    def __init__(self, found: _Found, done: set[str]) -> None:
        """Fill ``found``, leaving ``done``'s fields as they are."""
        self.found, self.done = found, done

    def add(self, name: str, value: str, span: str, s: int, e: int) -> None:
        """Add unless ``name`` was answered before."""
        if name not in self.done:
            self.found.add(name, value, span, s, e)

    def has(self, name: str) -> bool:
        """Whether ``name`` has an answer."""
        return self.found.has(name)


class Joined:
    """A record's passages joined into one text, and back: a JSON record read as lines."""

    def __init__(self, state: Sequence[Any]) -> None:
        """Join ``state``'s passages with newlines, remembering where each starts."""
        self.parts = list(state)
        self.starts = []
        at = 0
        for p in self.parts:
            self.starts.append(at)
            at += len(p.text) + 1
        self.text = "\n".join(p.text for p in self.parts)

    def ref(self, s: int, e: int) -> tuple[str, int, int] | None:
        """The passage holding ``[s, e)`` of the joined text, and the range inside it."""
        for p, at in zip(self.parts, self.starts, strict=True):
            if at <= s and e <= at + len(p.text):
                return p.span_id, s - at, e - at
        return None

    def carry(self, got: _Found, found: _Found) -> None:
        """``got``'s answers (offsets in the joined text) moved onto the passages."""
        for name, items in got.items.items():
            for value, r in items:
                if r.span_id != "@":
                    found.add(name, value, r.span_id, r.start, r.end)
                    continue
                where = self.ref(r.start, r.end)
                if where is None:  # a value across lines: its first line's part
                    first = self.ref(r.start, r.start + 1)
                    if first is None:
                        continue
                    span, a, _ = first
                    p = next(x for x in self.parts if x.span_id == span)
                    where = (span, a, len(p.text))
                found.add(name, value, *where)


class PublicationBiblio(RuleExtractionModel):
    """``publication_biblio``: a patent publication in JSON, ST.9 codes, labels or XML."""

    def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """Every bibliographic field the record states."""
        found = _Found()
        joined = Joined(request.state)
        if joined.text.lstrip()[:1] in "{[":
            got = _Found()
            data = _loads(joined.text)
            self._json("@", joined.text, got, data)
            self._tree(joined.text, data, got)
            if not (got.has("title") or got.has("abstract") or got.has("claims")):
                got = _Found()  # a number alone (a priority claim's) is no publication
            joined.carry(got, found)
        else:
            self._text(request.state, found)
            self._fields(request.state, found)
        if not found.has("number"):
            return ExtractionResult(
                tuple((f.name, FieldAnswer(None, (), p_null_ppm=ABSENT)) for f in request.fields)
            )
        return found.result(request)

    def _tree(self, text: str, data: Any, found: _Found) -> None:
        """Fields of a JSON record by the meaning of their keys, wherever they nest.

        The vocabulary of bibliographic data in general (a publication number, its kind
        and office, a priority claim, filing and grant events, a status said by a source,
        claims): a key names what its value is, or an event names what its date is.
        """
        leaves = list(_leaves(data))
        pos = 0
        spans: list[tuple[str, str, int, int, Any]] = []
        for path, value, parent in leaves:
            at = _where(text, value, pos) if isinstance(value, str) and value else None
            if at is None:
                continue
            pos = at[1]
            spans.append((path, value, at[0], at[1], parent))
        had = {k for k, v in found.items.items() if v}
        for path, value, s0, e0, parent in spans:
            last = path.rsplit(".", 1)[-1]
            field = biblio_field(last)
            if field is None and "claims" in path.split(".") and last in ("text", "claim"):
                field = "claims"
            if field in had:
                continue
            if field is None and isinstance(parent, dict):
                said = " ".join(str(v).lower() for v in parent.values() if isinstance(v, str))
                if parse_date(value[:10]) and last in ("date", "when", "on", "as_of"):
                    for word, name in EVENT_DATES:
                        if re.search(rf"\b{word}", said):
                            field = name
                            break
                elif last in ("says", "text", "status", "wording") and re.search(
                        r"status|register|legal|front page|code", " ".join(map(str, parent)) +
                        " " + said):  # fmt: skip
                    field = "status"
            if field is None:
                continue
            if field == "priority_claims":
                if not _ID_VALUE.match(value) and not isinstance(parent, dict):
                    continue
                if isinstance(parent, dict):
                    s0, e0 = _enclosing(text, s0)
                found.add(field, text[s0:e0], "@", s0, e0)
                continue
            if field in ("filing_date", "grant_date", "publication_date", "priority_date"):
                iso = parse_date(value[:10])
                if iso is None or found.has(field):
                    continue
                found.add(field, iso, "@", s0, e0)
                continue
            if field in ("claims", "status") or not found.has(field):
                found.add(field, value, "@", s0, e0)
        for path, value, s0, _e0, parent in spans:  # a priority event in a timeline, or a list
            values = [str(v).lower() for v in (parent.values() if isinstance(parent, dict) else ())]
            named = "priority" in values or any("priorit" in p for p in path.split(".")[:-1])
            dict_ = isinstance(parent, dict)
            if named and dict_ and _ID_VALUE.match(value) and value != "priority" \
                    and "priority_claims" not in had:  # fmt: skip
                a, b = _enclosing(text, s0)
                found.add("priority_claims", text[a:b], "@", a, b)

    def _fields(self, state: Sequence[Any], found: Any) -> None:
        """Fields written as ``key: value`` lines, XML elements, list items or term pairs."""
        lines = [parse_line(sp.span_id, sp.text) for sp in state]
        claims_on = False
        pre = {k for k, v in found.items.items() if v}
        found = _Only(found, pre)
        for i, ln in enumerate(lines):
            body = ln.text[ln.body[0] : ln.body[1]]
            key, value, vs = "", "", ln.body[0]
            titled = _NUMBER_KIND.search(body) if len(body) <= 120 else None
            if titled and not found.has("number"):  # "XX-1234 A1: ...": number, kind code
                found.add("number", titled.group(1), ln.span, ln.body[0] + titled.start(1),
                          ln.body[0] + titled.end(1))  # fmt: skip
                found.add("kind_code", titled.group(2), ln.span, ln.body[0] + titled.start(2),
                          ln.body[0] + titled.end(2))  # fmt: skip
            if ln.kind == "heading":
                claims_on = bool(re.match(r"\s*claims?\b", body, re.I))
                continue
            lead = re.match(r"(Abstract|Description)(?:\s*\([^)]*\))?[.:]\s+(?=\S)", body)
            if ln.kind == "paragraph" and lead:  # "Abstract. A robot wrist ..."
                field = lead.group(1).lower()
                if not found.has(field):
                    found.add(field, body[lead.end() :], ln.span, ln.body[0] + lead.end(),
                              ln.body[1])  # fmt: skip
                continue
            if ln.kind == "paragraph" and re.match(r"Claims\b", body):
                claims_on = True
                continue
            if ln.kind == "meta":
                key, value = ln.marker, body
            elif ln.kind == "item":
                kv = re.match(r"([A-Za-z][\w .-]{0,30}?)(?:\s*\([^)]*\))?:\s+(.+)$", body)
                if kv:
                    key, value, vs = kv.group(1).lower(), kv.group(2), ln.body[0] + kv.start(2)
                elif claims_on or ln.text.lstrip().startswith("<claim"):
                    found.add("claims", body, ln.span, *ln.body)
                    continue
            elif ln.kind == "paragraph" and i + 1 < len(lines) and biblio_field(body.strip()):
                nxt = lines[i + 1]  # a term and its definition: "Office" then "SYN-Office-1"
                key, value = body.strip().lower(), nxt.text[nxt.body[0] : nxt.body[1]]
                ln, vs = nxt, nxt.body[0]
            if not key:
                continue
            key = key.replace(" ", "_")
            field = biblio_field(key)
            if field is None:
                continue
            ve = vs + len(value)
            if field in ("filing_date", "grant_date", "publication_date", "priority_date"):
                dates = find_dates(value, "dmy")
                if dates and not found.has(field):
                    iso, ds, de = dates[0]
                    found.add(field, iso, ln.span, vs + ds, vs + de)
                if field != "priority_date":
                    continue
            if field in ("priority_claims", "priority_date") and re.search(
                r"[A-Z]+-\w*\d", ln.text
            ):
                m = re.search(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d[\w-]*", ln.text)
                if m and not found.has("priority_claims"):
                    a = m.start()
                    found.add("priority_claims", ln.text[a : ln.body[1]].strip(), ln.span, a,
                              a + len(ln.text[a : ln.body[1]].strip()))  # fmt: skip
                continue
            if field == "priority_claims":
                continue
            if field in ("claims", "status") or not found.has(field):
                found.add(field, value, ln.span, vs, ve)

    def _json(self, span: str, text: str, found: _Found, data: Any = None) -> None:
        if data is None:
            data = _loads(text)
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
            if re.fullmatch(r"(?:#+\s*)?(?:\*\*)?Claims(?:\*\*)?(?:\s*\([^)]*\))?[.:]?", stripped,
                            re.I):  # fmt: skip
                in_claims, in_desc = True, False
                continue
            if re.fullmatch(r"(?:#+\s*)?(?:\*\*)?(?:Description|Detailed description)(?:\*\*)?"
                            r"(?:\s*\([^)]*\))?[.:]?", stripped, re.I):  # fmt: skip
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
    "candidate": ("candidate", "invention", "project", "idea", "ideas", "innovation",
                  "disclosure for"),
    "contributed": ("contributed", "contribution", "contributions", "what they did", "work",
                    "did", "part", "worked", "credited", "credits", "credit"),
    "agreement": ("agreement", "agreements", "ip", "terms", "contract", "assignment"),
    "source": ("source", "link", "file", "evidence", "record", "document", "doc", "where"),
    "event_type": ("type", "kind", "channel", "format", "venue", "occasion"),
    "content_shown": ("shown", "what", "summary", "description", "content", "material",
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
    vals = [v for v in values if v and v.lower() not in ("null", "none", "n/a", "-")]
    ids = bool(vals) and all(_ID_VALUE.match(v) for v in vals)
    if "id" in words or ids:
        if {"event", "disclosure", "ev"} & set(words):
            return "disclosure_id"
        if {"person", "contributor", "inventor", "people", "member"} & set(words):
            return "person_id"
        if {"candidate", "invention", "project"} & set(words):
            return "candidate"
        if {"record", "row", "line"} & set(words) or words == ["id"]:
            return "record_id"
        if ids and {"event"} & set(words):
            return "disclosure_id"
        if ids and words in (["ref"], ["reference"], ["key"], ["no"]):
            return "record_id"
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
    terms = re.compile(r"\b(agreement|contract|assign\w*|ip clause|nda)\b", re.I)
    if vals and sum(bool(terms.search(v)) for v in vals) * 2 > len(vals):
        return "agreement"
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
                     r"workshop|stand-?up|retro|design sync|call transcript|speakers?:|"
                     r"speaker turns)", re.I)  # fmt: skip
EVENT = re.compile(r"\b(demo|demonstrat\w*|trade show|expo|conference|talk|webinar|field day|"
                   r"open day|poster|blog post|press release|pilot|shown|showed|audience|"
                   r"disclosure log|posted|video|public device|public database|published on|"
                   r"release notes|sharing register|disclosure register)\b", re.I)  # fmt: skip
PRIOR_ART = re.compile(r"\b(prior[- ]art|search report|search export|patent search)\b", re.I)
RECORD_KEYS = re.compile(r"(?:^|\s)([A-Z][A-Za-z ]{1,20}):\s")
PUBLICATION = re.compile(r"\(1[19]\)|\(2[12]\)|\(5[147]\)|\bpublication (?:number|no)\b|"
                         r"\bkind code\b", re.I)  # fmt: skip
MECHANISM = re.compile(r"\b(" + "|".join(re.escape(w) for w in HEADINGS["mechanism"]) + r")\b",
                       re.I)  # fmt: skip
STEPS = re.compile(r"(?:^|\s)(?:\d{1,2}[.)]|[A-Z]{1,2}\d{1,2}[.)]|step \d+[:.]?)\s+[A-Z]")


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
    shape = got.get("structure", "")
    if fmt == "xml" and re.search(r"\bitem (step|steps)\b", shape):
        return "invention_disclosure", SURE
    if fmt == "xml" and re.search(r"\b(claim|claims|abstract)\b", shape) and \
            re.search(r"\b(doc-number|publication|kind)\b", shape):  # fmt: skip
        return "prior_art_publication", SURE
    if fmt in ("csv", "json", "jsonl", "yaml"):
        names = " ".join(cols).lower()
        if (re.search(r"\b(publication|pub|patent|doc|document)[_ ]?(number|no|num)\b|"
                      r"\bkind_code\b", fields) and re.search(r"\b(abstract|title|claims?)\b",
                                                             fields + " " + names)) or (
                re.search(r"\babstract\b", names) and re.search(r"\bclaims\b", names)):  # fmt: skip
            return "prior_art_publication", SURE
        if any(role_of(c.rsplit(".", 1)[-1]) == "mechanism" for c in cols):
            return "invention_disclosure", SURE
        roles = {guess_role(c, v) for c, v in cols.items()}
        if ({"person_id", "name"} & roles) and ("contributed" in roles or (
                "content_shown" in roles and "date" not in roles)):  # fmt: skip
            return "contributor_record", SURE
        if "date" in roles and "content_shown" in roles:
            return "disclosure_record", SURE
        if "rank" in roles and re.search(r"\b(goal|priorit|objective|aim|focus)", " ".join(cols),
                                         re.I):  # fmt: skip
            return "business_priorities", SURE
        return "other", SURE
    text = got.get("text", "")
    heads = got.get("headings", "")
    lead = heads + " " + text[:400]
    found: list[str] = []
    if PRIOR_ART.search(heads + " " + text[:200]) and re.search(r"\bclaims?\b", text, re.I):
        return "prior_art_publication", SURE
    keys = RECORD_KEYS.findall(text)
    repeated = {k for k in keys if keys.count(k) >= 2}
    if len(repeated) >= 3 and any(re.search(r"date|when", k, re.I) for k in repeated) and \
            EVENT.search(heads + " " + text[:200] + " " + " ".join(repeated)):  # fmt: skip
        return "disclosure_record", SURE
    ranked = re.findall(r"\b(?:priority|rank|level|P|O)\s?(\d)\b", heads + " " + text, re.I)
    if (PRIORITY_WORDS.search(heads) or len(set(ranked)) >= 3) and len(set(ranked)) >= 2 \
            and not re.search(r"\bclaims\b", text, re.I):  # fmt: skip
        return "business_priorities", SURE
    if PUBLICATION.search(text[:1500]) or (re.search(r"\bclaims\b", heads, re.I)
                                           and re.search(r"\babstract\b", text, re.I)):  # fmt: skip
        return "prior_art_publication", SURE
    if FORM_TITLE.search(lead) and int(got.get("table rows", "0") or 0) >= 3:
        return "form_template", SURE
    if FORM_TITLE.search(heads) and re.search(r"_{3,}|\[ \]|:\s*$", text):
        return "form_template", SURE
    if FORM_TITLE.search(heads) and (text.count("?") >= 3 or re.search(r"\bblank\b", lead, re.I)):
        return "form_template", SURE
    if PRIORITY_WORDS.search(heads) and re.search(r"\brank|\b1[.)]\s", text, re.I):
        return "business_priorities", SURE
    mechanism = bool(MECHANISM.search(heads)) or len(STEPS.findall(text)) >= 2
    if fmt == "email":
        return "email", SURE
    if (MEETING.search(heads + " " + shape) or re.search(r"\b(attendees|present|participants|"
                                                         r"speakers?|call)\s*:", text[:600], re.I)
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
