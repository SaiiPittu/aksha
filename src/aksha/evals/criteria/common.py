"""Helpers the criteria share: finding outputs for keyed entities, spans and overlap.

Every span comparison happens in Unicode code points of the raw source file (eval design
section 3.2). A citation's span is its ``start`` and ``end`` when the file holds exactly the
quote there; otherwise the quote's unique occurrence in the file; otherwise it has none.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from aksha.evals.grade import GradeContext
from aksha.evals.key import Anchor, KeyFact
from aksha.evals.normalize import name_key, squash
from aksha.evals.state import Output, walk_citations

SPAN_CAP = 600
"""A tight citation is at most this many code points, or three times its anchor's length."""
MIN_PRECISION_PPM = 500_000
"""A tight citation spends at least half its length on the anchored fact."""


@dataclass(frozen=True)
class Span:
    """A citation located in one version of its file."""

    file: str
    start: int
    end: int

    @property
    def length(self) -> int:
        """Code points covered."""
        return max(0, self.end - self.start)


def locate(ctx: GradeContext, cite: Mapping[str, Any]) -> Span | None:
    """Where ``cite`` sits in its file (module docstring); None when it is not there."""
    file, exact = str(cite.get("file", "")), str(cite.get("exact", ""))
    if not exact:
        return None
    raw_start, raw_end = cite.get("start"), cite.get("end")
    start = raw_start if isinstance(raw_start, int) else None
    end = raw_end if isinstance(raw_end, int) else None
    for text in ctx.file_texts(file):
        held = start is not None and end is not None and 0 <= start <= end <= len(text)
        if held and squash(text[start:end]) == squash(exact):
            assert start is not None and end is not None
            return Span(file, start, end)
        if text.count(exact) == 1:
            at = text.index(exact)
            return Span(file, at, at + len(exact))
        if start is not None and end is not None and text.count(exact) > 1:
            continue  # an ambiguous quote whose offsets do not hold it
        compact = squash(exact)
        if compact and squash(text).count(compact) == 1 and text.count(exact) == 0:
            return Span(file, -1, -1)  # found modulo whitespace; position unknown
    return None


def overlap(span: Span, anchor: Anchor) -> int:
    """Code points shared by a located span and a keyed anchor in the same file."""
    if span.file != anchor.file or span.start < 0:
        return 0
    return max(0, min(span.end, anchor.end) - max(span.start, anchor.start))


def tight(span: Span, anchor: Anchor) -> tuple[bool, int]:
    """Whether the span overlaps the anchor tightly enough, and its precision in ppm."""
    shared = overlap(span, anchor)
    if shared == 0 or span.length == 0:
        return False, 0
    precision = shared * 1_000_000 // span.length
    cap = max(SPAN_CAP, 3 * (anchor.end - anchor.start))
    return precision >= MIN_PRECISION_PPM and span.length <= cap, precision


def cites(ctx: GradeContext, citations: Iterable[Mapping[str, Any]], fact: KeyFact) -> bool:
    """Whether any of ``citations`` overlaps ``fact``'s anchor tightly."""
    if fact.anchor is None:
        return False
    for c in citations:
        span = locate(ctx, c)
        if span is not None and tight(span, fact.anchor)[0]:
            return True
    return False


def all_citations(out: Output) -> list[Mapping[str, Any]]:
    """An output's own citations and every citation inside its content."""
    return [*out.citations, *walk_citations(out.content)]


def competitor_output(ctx: GradeContext, kid: str) -> list[Output]:
    """The competitor outputs that stand for keyed competitor ``kid``.

    An output stands for it when its name or an alias matches the key's canonical name or
    aliases, or when it cites one of the key's pages for that competitor.
    """
    entry = dict(ctx.key.resolve(f"competitors.{kid}"))
    names = {name_key(entry.get("canonical", ""))} | {name_key(a) for a in entry.get("aliases", ())}
    pages = set(entry.get("pages", ()))
    found = []
    for out in ctx.state.of_kind("competitor"):
        own = {name_key(str(out.content.get("name", "")))}
        own |= {name_key(str(a)) for a in out.content.get("aliases", ())}
        files = {str(c.get("file")) for c in all_citations(out)}
        if own & names or files & pages:
            found.append(out)
    return found


def field_of(out: Output | None, name: str) -> Mapping[str, Any] | None:
    """A field of an extraction-style output (``content.fields.<name>``), or None."""
    if out is None:
        return None
    fields = out.content.get("fields", {})
    value = fields.get(name) if isinstance(fields, Mapping) else None
    return value if isinstance(value, Mapping) else None


def text_of(out: Output) -> str:
    """Every string an output holds, joined: what a text check scans for one output."""
    from aksha.evals.state import walk_strings

    return "\n".join(walk_strings(out.content))
