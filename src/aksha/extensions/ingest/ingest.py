"""Intake (design note 3.1): bytes in, typed from content, converted, told.

:func:`ingest` runs the whole of section 3.1 for one input, all code, no model:

1. ``received``: the byte cap. Over it is a :class:`~aksha.extensions.ingest.types.Refusal`
   and nothing is stored.
2. ``stored``: the original bytes go to ``put`` (the blob store), by hash.
3. ``typed``: :func:`~aksha.extensions.ingest.sniff.sniff`; the allow-list runs on the
   detected type, so a ZIP renamed ``.pdf`` is judged as a ZIP.
4. ``parse_fast``: the converter declared for the detected type. Each converter that needs
   an optional library degrades to a gap ``unsupported`` naming the extra; a converter
   that raises is a failed rung, so the document is a gap ``unreadable`` that names the
   error, and the upload goes on.
5. Children: each email attachment is ingested as its own document (``attachment``,
   ``parent`` set) under ``Caps.max_depth`` and ``Caps.max_children``; past either it is
   refused by name.
6. The header (:func:`~aksha.extensions.ingest.header.render_header`) and the record text
   the model reads: the header, a blank line, then the representation.

``key_for`` names each child: it is called with the parent's doc key, the child's index
among its siblings (from 1), its name and its bytes, and returns the child's doc key and
version, so a caller that keeps records by path can give a re-uploaded attachment its old
key. ``profile`` is the image profile of the model that
will read the document (None: no vision-capable model configured), used to resolve an
image's route and prepare its sent copy.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field

from aksha.extensions.ingest import images, mail, office, pdf, sniff, text
from aksha.extensions.ingest.header import render_header
from aksha.extensions.ingest.profiles import SentCopyCache, prepare_image
from aksha.extensions.ingest.routing import resolve_route
from aksha.extensions.ingest.types import (
    Caps,
    Converted,
    Document,
    Gap,
    ImageProfile,
    Origin,
    Refusal,
    Representation,
    Route,
)

AS_GIVEN_TYPES: frozenset[str] = frozenset(
    {sniff.TEXT, sniff.MARKDOWN, sniff.CSV, sniff.TSV, sniff.JSON, sniff.HTML, sniff.SVG}
)
"""Text types whose UTF-8 file an agent's own tools parse as it is (served unchanged)."""


KeyFn = Callable[[str, int, str, bytes], tuple[str, int]]
"""``(parent doc key, index from 1, name, bytes) -> (doc key, version)`` for a child."""


@dataclass(frozen=True)
class Ingested:
    """One input after intake: its document, its representation or gap, its children."""

    document: Document
    representation: Representation | None
    gap: Gap | None
    header: str
    route: Route | None = None
    frames: tuple[images.Frame, ...] = field(default=(), repr=False)
    children: tuple[Ingested, ...] = ()
    refusals: tuple[Refusal, ...] = ()

    @property
    def record_text(self) -> str:
        """What the model reads: the header, then the representation (header only for a gap)."""
        if self.representation is None:
            return self.header + "\n"
        return f"{self.header}\n\n{self.representation.text}"

    @property
    def as_given(self) -> bool:
        """Whether the file is UTF-8 text an agent's tools parse unchanged (no header).

        That is a type in :data:`AS_GIVEN_TYPES`, or an email with no attachments, decoded as
        UTF-8 with no type conflict and no gap.
        """
        d = self.document
        if self.gap is not None or d.charset != "utf-8" or d.type_conflict is not None:
            return False
        if d.detected_type == sniff.EMAIL:  # nothing to convert without attachments
            return not self.children and not self.refusals
        return d.detected_type in AS_GIVEN_TYPES

    def walk(self) -> tuple[Ingested, ...]:
        """This document and every descendant, depth first."""
        out: list[Ingested] = [self]
        for c in self.children:
            out.extend(c.walk())
        return tuple(out)


def _convert(
    found: sniff.Sniffed, data: bytes, caps: Caps
) -> tuple[Converted, tuple[images.Frame, ...]]:
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


def ingest(
    name: str,
    data: bytes,
    origin: Origin = "upload",
    *,
    doc_key: str,
    version: int = 1,
    parent: str | None = None,
    declared_type: str | None = None,
    caps: Caps | None = None,
    allow: frozenset[str] = sniff.SUPPORTED,
    key_for: KeyFn | None = None,
    put: Callable[[bytes], str] | None = None,
    profile: ImageProfile | None = None,
    cache: SentCopyCache | None = None,
    depth: int = 0,
) -> Ingested | Refusal:
    """Run intake for one input (module doc)."""
    caps = caps or Caps()
    if len(data) > caps.max_bytes:
        return Refusal("max_bytes", len(data), caps.max_bytes, name, origin)
    blob = put(data) if put is not None else "sha256:" + hashlib.sha256(data).hexdigest()
    found = sniff.sniff(data, name, declared_type, caps=caps)
    doc = Document(doc_key, version, blob, name, origin, parent, declared_type,
                   found.detected_type, found.charset, found.type_conflict,
                   len(data))  # fmt: skip
    gap = found.gap
    if gap is None and found.detected_type not in allow:
        label = sniff.LABELS.get(found.detected_type, found.detected_type)
        gap = Gap("refused", f"type not allowed: {label}")
    if gap is not None:
        return Ingested(doc, None, gap, render_header(doc, None, gap))
    try:
        converted, frames = _convert(found, data, caps)
    except Exception as e:  # a parser bug or a hostile file: a failed rung, never a crash
        converted = Converted(gap=Gap("unreadable", f"the converter failed: {type(e).__name__}",
                                      ("parse_fast",)))  # fmt: skip
        frames = ()
    if converted.gap is not None or converted.representation is None:
        gap = converted.gap or Gap("unreadable", "the converter returned nothing")
        return Ingested(doc, None, gap, render_header(doc, None, gap))
    rep = converted.representation
    children: list[Ingested] = []
    refusals: list[Refusal] = []
    for n, child in enumerate(converted.children, start=1):
        if depth + 1 > caps.max_depth:
            refusals.append(Refusal("max_depth", depth + 1, caps.max_depth, child.name,
                                    "attachment"))  # fmt: skip
            continue
        if n > caps.max_children:
            refusals.append(Refusal("max_children", n, caps.max_children, child.name,
                                    "attachment"))  # fmt: skip
            continue
        if key_for is None:
            raise ValueError("an input with attachments needs key_for to name them")
        key, ver = key_for(doc_key, n, child.name, child.data)
        got = ingest(child.name, child.data, "attachment", doc_key=key, version=ver,
                     parent=doc_key, declared_type=child.declared_type, caps=caps, allow=allow,
                     key_for=key_for, put=put, profile=profile, cache=cache,
                     depth=depth + 1)  # fmt: skip
        if isinstance(got, Refusal):
            refusals.append(got)
        else:
            children.append(got)
    if refusals:
        named = ", ".join(f"{r.name_as_given!r} ({r.cap})" for r in refusals)
        rep = Representation(rep.text, rep.anchors, rep.converters, rep.pages,
                             (*rep.dropped, f"attachments refused: {named}"), rep.signals,
                             rep.page_count, rep.image, rep.described, rep.notes)  # fmt: skip
    route: Route | None = None
    if frames:
        first = frames[0]
        route = resolve_route(
            doc, profile,
            prepare=lambda p: prepare_image(blob.split(":", 1)[1], first, p, cache=cache),
        )  # fmt: skip
    header = render_header(doc, rep, None, route=route)
    return Ingested(doc, rep, None, header, route, tuple(frames), tuple(children),
                    tuple(refusals))  # fmt: skip
