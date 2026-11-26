"""The ingestion records (design note section 6.1): documents, gaps, representations, images.

Every value is frozen and holds integers and strings only, so each one can be written into
an event payload as it is (the event store refuses floats, ADR-P01 D4). Offsets count code
points of :attr:`Representation.text`.

- A :class:`Document` is the stored original bytes of one input, typed from its content.
- A :class:`Gap` is a document-level dead end with a reason; the model is told about it.
- A :class:`Refusal` is a cap that failed before anything was stored.
- A :class:`Representation` is the text rendering of one document version, with anchors
  (``p.3``, ``slide 2``, ``Sheet1!A1:F200``, a heading), the rung and trust of each page,
  and what was dropped, by name.
- An image has an original (never sent), a sent copy per :class:`ImageProfile`
  (:class:`SentImage`) and a text sidecar, which is its representation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Origin = Literal["upload", "message", "tool_result", "attachment"]
Rung = Literal["parse_fast", "parse_layout", "parse_vision"]
Trust = Literal["extracted", "engine_written", "model_written"]
GapReason = Literal["refused", "locked", "unsupported", "unreadable", "cancelled"]
Setup = Literal["s2_only", "s1_decide", "s1_extract", "s1_decide_extract"]
RouteKind = Literal["text", "native_file", "native_image", "header_only"]
CostKind = Literal["patch", "flat"]

GAP_REASONS: tuple[str, ...] = ("refused", "locked", "unsupported", "unreadable", "cancelled")


@dataclass(frozen=True)
class Caps:
    """Every cap ingestion applies, in one value (placeholders, unmeasured, 26 Nov 2026).

    ``max_bytes`` is per file; ``max_zip_bytes`` and ``max_zip_entries`` bound what an
    Office container expands to; ``max_pages`` bounds a PDF or a TIFF; ``min_chars_per_page``
    is the floor under which a PDF page reads as a scan; ``max_garbled_ppm`` is the share of
    replacement characters over which a page reads as a broken font encoding;
    ``max_pixels`` is the decoded-pixel cap checked from the image header before decoding,
    a budget across all of a multi-page image's kept pages;
    ``max_cells`` is per spreadsheet sheet and ``max_book_cells`` per workbook (an empty
    row counts as one); ``max_depth`` is how deep email attachments may
    nest; ``max_children`` is how many attachments one email may add;
    ``result_cap_chars`` is the tool-result cap over which a result becomes a document
    (ADR-P08 D4); ``message_cap_chars`` the user-message size over which a message is
    stored and paged (ING-4); ``head_chars`` and ``tail_chars`` are the view an offloaded
    result keeps; ``fallback_charset`` decodes text that is neither BOM-marked nor UTF-8.
    """

    max_bytes: int = 8 * 1024 * 1024
    max_zip_bytes: int = 100 * 1024 * 1024
    max_zip_entries: int = 10_000
    max_pages: int = 2_000
    min_chars_per_page: int = 20
    max_garbled_ppm: int = 50_000
    max_pixels: int = 40_000_000
    max_cells: int = 200_000
    max_book_cells: int = 1_000_000
    rows_per_anchor: int = 200
    max_depth: int = 2
    max_children: int = 50
    result_cap_chars: int = 8_000
    message_cap_chars: int = 32_000
    head_chars: int = 4_000
    tail_chars: int = 4_000
    fallback_charset: str = "latin-1"


@dataclass(frozen=True)
class Document:
    """One input's stored original: a record of the ``documents`` source (ADR-P05).

    ``blob`` is ``sha256:<hex>`` of the original bytes. ``name_as_given`` is never trusted
    as a path and is escaped before it is rendered. ``parent`` is the email's doc key for an
    attachment, the step ID for a tool result. ``type_conflict`` says when the claimed type
    or the extension and the bytes disagree; the bytes win.
    """

    doc_key: str
    version: int
    blob: str
    name_as_given: str
    origin: Origin
    parent: str | None
    declared_type: str | None
    detected_type: str
    charset: str | None
    type_conflict: str | None
    size_bytes: int

    def as_json(self) -> dict[str, object]:
        """The document as an event payload carries it."""
        return {
            "doc_key": self.doc_key,
            "version": self.version,
            "blob": self.blob,
            "name": self.name_as_given,
            "origin": self.origin,
            "parent": self.parent,
            "declared_type": self.declared_type,
            "detected_type": self.detected_type,
            "charset": self.charset,
            "type_conflict": self.type_conflict,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class Gap:
    """A document-level dead end: ``reason`` is one of :data:`GAP_REASONS`.

    ``detail`` names the cap, the missing rung or the missing extra (``"empty"``,
    ``"decompression_cap"``, ``"OCR not available"``). ``steps`` are the rungs tried.
    """

    reason: GapReason
    detail: str
    steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class Refusal:
    """A cap that failed before storing: no doc key, nothing kept."""

    cap: str
    value: int
    limit: int
    name_as_given: str
    origin: Origin


@dataclass(frozen=True)
class Anchor:
    """A named position in a representation: ``text[start:end]`` is what it labels."""

    label: str
    start: int
    end: int


@dataclass(frozen=True)
class PageRung:
    """Which rung read one page (or slide, sheet, section), at what trust."""

    anchor: str
    rung: Rung
    trust: Trust


@dataclass(frozen=True)
class ImageInfo:
    """Facts about an image document, set on its representation."""

    format: str
    source_wh: tuple[int, int]
    frames: int
    orientation_applied: bool
    metadata_stripped: bool
    frames_dropped: int = 0


@dataclass(frozen=True)
class Representation:
    """The text rendering of one document version (content of ``doc.representation``).

    ``converters`` are the libraries and versions that read it (``"pypdf 7.1.1"``);
    ``pages`` say which rung read each page; ``dropped`` names, in words, what is not in
    ``text`` (``"pages 7-10: scanned, not read; OCR not available"``); ``signals`` are the
    integer quality signals per page; ``notes`` are short facts a header prints on its
    ``converted:`` line (``"delimiter ';'"``). For an image, ``text`` is the sidecar and
    ``described`` says whether an OCR or vision rung wrote any of it.
    """

    text: str
    anchors: tuple[Anchor, ...] = ()
    converters: tuple[str, ...] = ()
    pages: tuple[PageRung, ...] = ()
    dropped: tuple[str, ...] = ()
    signals: tuple[tuple[str, int], ...] = ()
    page_count: int | None = None
    image: ImageInfo | None = None
    described: bool = True
    notes: tuple[str, ...] = ()

    def anchor(self, label: str) -> Anchor | None:
        """The anchor labelled ``label``, or None."""
        return next((a for a in self.anchors if a.label == label), None)

    def trusts(self) -> tuple[str, ...]:
        """The distinct trust levels of the pages, in order of first appearance."""
        seen: list[str] = []
        for p in self.pages:
            if p.trust not in seen:
                seen.append(p.trust)
        return tuple(seen)


@dataclass(frozen=True)
class Child:
    """An attachment or a page an intake split out, to be ingested as its own document."""

    name: str
    data: bytes
    declared_type: str | None = None


@dataclass(frozen=True)
class Converted:
    """What one converter returns: a representation, children to ingest, or a gap."""

    representation: Representation | None = None
    gap: Gap | None = None
    children: tuple[Child, ...] = ()


@dataclass(frozen=True)
class ImageProfile:
    """How one target model takes images (the extension's table until ADR-P07, ING-9).

    ``cost`` ``patch``: tokens are ``ceil(w/patch_px) * ceil(h/patch_px)`` patches times
    ``token_multiplier_ppm``; ``flat``: every image costs ``flat_tokens``. ``max_patches``
    is the patch budget the provider shrinks to; ``reject_over_patches`` is a count the
    provider refuses outright, so nothing is ever sent over it. ``many_image_threshold``
    and ``many_image_max_edge`` are the stricter per-image limit once a request holds more
    images than the threshold, counted across the whole request; the edge is configuration
    because it has changed over time. ``fit`` is the resize rule: ``patch_budget`` (the
    largest aspect-preserving size whose padded sides and patch count fit),
    ``patch_area`` (fit the long edge, then scale by area to the budget and round the grid
    down) or ``edge`` (only
    the long edge). ``as_of`` is the date the limits were read.
    """

    profile_id: str
    accepts: frozenset[str]
    cost: CostKind
    fit: Literal["patch_budget", "patch_area", "edge"]
    patch_px: int | None
    max_long_edge: int
    max_patches: int | None
    reject_over_patches: int | None
    token_multiplier_ppm: int
    flat_tokens: int | None
    many_image_threshold: int | None
    many_image_max_edge: int | None
    max_bytes_per_image: int
    resize_in_tool_results: bool
    ref_kinds: frozenset[str]
    as_of: str = "2026-11-26"


@dataclass(frozen=True)
class SentImage:
    """A sent copy of one image for one profile; derived, re-derivable, cached.

    ``scale_ppm`` maps a point the model returns back to the original
    (``x_original = x_sent * 1_000_000 / scale_ppm``). ``passed_through`` is True when the
    original bytes fit as they were and went out unchanged. ``blob`` is the sent bytes'
    ``sha256:`` hash, which a rendering records so a replay sends the same bytes.
    """

    source_sha256: str
    profile_id: str
    source_wh: tuple[int, int]
    sent_wh: tuple[int, int]
    scale_ppm: int
    media_type: str
    passed_through: bool
    blob: str
    est_tokens: int
    formula: str
    data: bytes = field(default=b"", repr=False, compare=False)


@dataclass(frozen=True)
class ImagePlan:
    """What one image block of a request does: ``send``, ``rederive`` or ``ref_only``."""

    index: int
    action: Literal["send", "rederive", "ref_only"]
    max_edge: int | None
    reason: str | None = None


@dataclass(frozen=True)
class Route:
    """What one consuming step receives for one document (design note section 3.0)."""

    kind: RouteKind
    sent: SentImage | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Binding:
    """Which model fills one contract: an alias's class, its model and whether it is native.

    ``adapted`` is True when the general model fills a System 1 contract under a forced
    schema (``native=False``). ``routed`` is False in setup 1, where there is no routing
    step: every request goes straight to the general model.
    """

    contract: Literal["decide", "extract", "generate"]
    alias: str
    model_class: Literal["classification", "extraction", "generation"]
    adapted: bool
    routed: bool
