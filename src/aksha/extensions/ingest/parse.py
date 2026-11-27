"""The parsing half of intake: the type check and the converter for one input's bytes.

:func:`parse_in_process` is steps 3 and 4 of :mod:`aksha.extensions.ingest.ingest`: the
sniffer, then, when the detected type has no gap and is allowed, the converter declared for
it. Both read untrusted bytes with third-party parsers, so the runtime runs this function
in a worker process (:mod:`aksha.extensions.ingest.worker`) under a wall-clock, memory and
CPU cap; :func:`in_process` runs it here, for library callers and tests.

A sniffer or converter that raises is a failed rung, never a crash: a gap ``unreadable``
naming the error, as before. A :class:`MemoryError` is not caught here: in the worker it is
the ``parser_memory`` gap, and :func:`in_process` turns it into the same gap.

``sniffed`` is called once the type check is done, before the converter runs, so a worker
that is killed while converting still reports what the bytes were.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from aksha.extensions.ingest import images, mail, office, pdf, sniff, text
from aksha.extensions.ingest.types import Caps, Converted, Gap

PARSER_TIMEOUT = "parser_timeout"
PARSER_MEMORY = "parser_memory"
PARSER_CRASHED = "parser_crashed"
PARSER_UNCONTAINED = "parser_uncontained"
PARSER_GAPS: tuple[str, ...] = (PARSER_TIMEOUT, PARSER_MEMORY, PARSER_CRASHED, PARSER_UNCONTAINED)
"""The named gaps a parser that hit a limit, died or could not be contained leaves
(``Gap.detail`` starts with one)."""


@dataclass(frozen=True)
class Parsed:
    """What parsing returned: the type check, and the conversion when one ran.

    ``converted`` is None when the type check left a gap or the detected type is not in the
    allow-list, so no converter ran.
    """

    found: sniff.Sniffed
    converted: Converted | None = None
    frames: tuple[images.Frame, ...] = field(default=(), repr=False)


Sniffed = Callable[[sniff.Sniffed], None]
Parse = Callable[[str, bytes, "str | None", Caps, frozenset[str]], Parsed]
"""``(name, bytes, declared type, caps, allow-list) -> Parsed``: one way to run parsing."""


def convert(
    found: sniff.Sniffed, data: bytes, caps: Caps
) -> tuple[Converted, tuple[images.Frame, ...]]:
    """The converter declared for ``found.detected_type``, run on ``data``."""
    kind = found.detected_type
    if kind in sniff.IMAGE_TYPES:
        decoded = images.decode(data, kind, caps)
        return decoded.converted, decoded.frames
    if kind == sniff.PDF:
        return pdf.convert(data, caps), ()
    if kind == sniff.DOCX:
        return office.docx(data), ()
    if kind == sniff.PPTX:
        return office.pptx(data), ()
    if kind == sniff.XLSX:
        return office.xlsx(data, caps), ()
    if kind == sniff.EMAIL:
        return mail.convert(data), ()
    body = found.text or ""
    if kind == sniff.MARKDOWN:
        return Converted(text.markdown(body)), ()
    if kind == sniff.JSON:
        return Converted(text.json_doc(body)), ()
    if kind in (sniff.CSV, sniff.TSV):
        return Converted(text.csv_doc(body, caps)), ()
    if kind == sniff.HTML:
        return Converted(text.html_doc(body)), ()
    if kind in (sniff.TEXT, sniff.SVG):
        return Converted(text.plain(body)), ()
    return Converted(gap=Gap("unsupported", f"no_converter: {kind}")), ()


def parse_in_process(
    name: str,
    data: bytes,
    declared_type: str | None,
    caps: Caps,
    allow: frozenset[str],
    *,
    sniffed: Sniffed | None = None,
) -> Parsed:
    """Type ``data``, then convert it when its type has no gap and is allowed (module doc)."""
    try:
        found = sniff.sniff(data, name, declared_type, caps=caps)
    except MemoryError:
        raise
    except Exception as e:  # a hostile container the sniffer cannot judge: never a crash
        found = sniff.Sniffed(sniff.OCTET, gap=Gap("unreadable", "the type check failed: "
                                                   f"{type(e).__name__}"))  # fmt: skip
    if sniffed is not None:
        sniffed(found)
    if found.gap is not None or found.detected_type not in allow:
        return Parsed(found)
    try:
        converted, frames = convert(found, data, caps)
    except MemoryError:
        raise
    except Exception as e:  # a parser bug or a hostile file: a failed rung, never a crash
        converted = Converted(gap=Gap("unreadable", f"the converter failed: {type(e).__name__}",
                                      ("parse_fast",)))  # fmt: skip
        frames = ()
    return Parsed(found, converted, frames)


def failed(found: sniff.Sniffed | None, gap: Gap) -> Parsed:
    """The result of a parse that hit a limit: ``gap`` on the conversion, or on the type."""
    if found is None:
        return Parsed(sniff.Sniffed(sniff.OCTET, gap=gap))
    if found.gap is not None:
        return Parsed(found)
    return Parsed(found, Converted(gap=gap))


def in_process(
    name: str, data: bytes, declared_type: str | None, caps: Caps, allow: frozenset[str]
) -> Parsed:
    """:func:`parse_in_process` in this process, with no time or memory cap of its own."""
    seen: list[sniff.Sniffed] = []
    try:
        return parse_in_process(name, data, declared_type, caps, allow, sniffed=seen.append)
    except MemoryError:
        gap = Gap("unreadable", f"{PARSER_MEMORY}: the parser ran out of memory", ("parse_fast",))
        return failed(seen[0] if seen else None, gap)
