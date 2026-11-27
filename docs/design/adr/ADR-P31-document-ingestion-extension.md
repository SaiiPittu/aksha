# ADR-P31 (draft number): Document ingestion and representations, an extension (decided shape: an extension that stores every input's original bytes, always converts them to a versioned text representation through a declared, per-page converter ladder, prepares images for each target model in plain code, extracts typed fields inside a reusable intake Task with or without System 1 models, and tells the model exactly what was done)

*Intermediate design as of 26 Nov 2026. Status: Draft, first milestone built. This is a design record; the built part is described in "Amendments from the build". The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md).*

## Status

Draft, 26 Nov 2026, revised after three reviews the same day and again after the maintainer's decisions that evening. The maintainer decided: ingestion is not a new primitive yet and ships as an extension under PR-04 and PR-08 (D1, ING-5); adapted extraction is allowed as an ADR-P07 amendment (D12, ING-2); a proposed schema is used only after review by default, with a per-agent opt-in to draft use within the run (D8, ING-3). The maintainer also corrected the routing picture: System 1 models are optional, and with none configured every request goes straight to the System 2 model (D12). The number P31 is kept only as a working label until the schema registry is built. No decisions-log entry covers it yet. The first-milestone build landed on 26 Nov 2026 as the extension `aksha.extensions.ingest`, wired into uploads; "Amendments from the build" below says what was built, where it differs from this text, and what is still designed only.

## Terms

- A **document** is the stored original bytes of one input (an upload, an email attachment, an oversized user message, a file returned by a tool, or a text tool result over ADR-P08's result cap), with its name as given, its detected media type (RFC 6838) and its SHA-256 hash.
- A **representation** is a text rendering of one document version, made by named converters, with page or section anchors and, per page, the rung that read it and the trust of its text. Citation spans are minted over it.
- A **rung** is one step of the converter ladder: fast text, layout or OCR (optical character recognition), vision model.
- **Trust** of text is `extracted` (from the file's own text layer or markup), `engine_written` (OCR output) or `model_written` (a vision model's transcription).
- A **document gap** is a document-level dead end (`refused`, `locked`, `unsupported`, `unreadable`, `cancelled`) with a reason. It is distinct from ADR-P13's explicit unknown, which is field-level.
- A **setup** is which model classes the configuration binds: setup 1, a System 2 general model only; setup 2, plus a System 1 decision model; setup 3, plus a System 1 extraction model.
- A **route** is what one consuming step receives for one document: the text representation, a native file or image block, or the header alone with a named gap.
- An image has an **original** (never sent), a **sent copy** sized for one model's **image profile**, and a **text sidecar** (OCR or description) that serves as its representation.
- A **document kind** is a type of document the agent can extract, with a JSON Schema whose leaf types are ADR-P07's field types, stored as a versioned `doc.schema` entry in a **schema registry**.

## Context: what breaks without it

| Without it | What happens | Primitive affected |
|---|---|---|
| Files passed to the model as native content blocks | The harness has no text of its own, so ADR-P13's verbatim check has nothing to run on; providers convert PDFs internally and differ in what they drop | PR-13 |
| Conversion with no record of what was done | The model cannot say "page 4 was a scan and I could not read it"; a silent drop looks like an absent value | PR-08, PR-13 |
| Detection by file extension | A ZIP renamed `.pdf` reaches a PDF parser; an allow-list judges the claimed type, not the real one | PR-11 |
| One converter, no fallback | A scanned or mixed PDF yields empty pages, read as "no value" | PR-13 |
| A general model filling extraction when no schema fits | A System 2 answer replaces a System 1 contract, so nothing is thresholdable | PR-07 |
| A design that assumes a System 1 model exists | An agent author with one model key cannot run extraction at all, or the harness routes through a decision model that is not there | PR-07 |
| Images sent unchanged | Each provider resizes on its own rule, so the stored image is not the image the model saw; one provider applies a stricter per-image size once a request holds more than 20 images, and rejects oversized tool-result screenshots instead of resizing them; a flat 85-token estimate undercounts by 15 to 55 times; EXIF GPS leaves the machine | PR-08, PR-14 |

## Decision

1. **D1, an extension, not hooks, and not a new primitive yet (decided 26 Nov 2026).** The ingest extension contributes, as declared data (ADR-P20 D3): three artifact kinds (`doc.representation`, `doc.schema`, `doc.extraction`), two reusable child Tasks (`doc.intake`, `doc.propose_schema`), two core tools (`read_document`, `list_documents`), a `documents` versioned source (ADR-P05), `ConverterSpec` entries, and an `ImageProfile` table keyed by model ID. Caps are PR-11 gate rules; rendering choices are `ContextPolicy` fields (ADR-P08 D9); post-model checks are ADR-P04 S1 validators and ADR-P13 D6 verifiers. No ordered middleware list exists; ordering would be the precedence ADR-P20 Alternative A rejected.
2. **D2, bytes first, typed from content.** Intake stores the bytes in the ADR-P04 D10 blob table by hash, then types them: magic bytes, then for text a byte-order mark, a strict UTF-8 decode and a declared fallback charset, then the extension only as a tie-breaker. Byte-size and count caps run before storing; the media-type allow-list and container expansion caps run on the detected type. Empty or unidentifiable input is a gap `unsupported` with that reason. An email's attachments re-enter intake as child documents under a depth cap. Documents are records of the `documents` source; a client replaces a document only by passing its doc key, and the key is never inferred from the name.
3. **D3, normalise always runs, per page.** Every document gets a text representation. Converters are tried cheapest first; code judges quality signals (characters per page, share of replacement characters, page-count mismatch) per page and moves only failing pages up. An upward edge needs the next rung to be declared; otherwise the pages are dropped, or the document is a gap `unreadable` whose reason names the missing rung. Each rung is a durable step (ADR-P02) whose `replaces` names the previous one, with page-count, decoded-stream, memory and time caps. Recovery that finds a rung started N times without completing marks it crashed and moves on. Cancel is checked at rung boundaries.
4. **D4, trust travels with the text.** Each page records its rung and trust, and every span minted over it carries that trust. The default cite policy for an exact-value field accepts only `extracted` spans; an `engine_written` or `model_written` span needs a declared verifier (ADR-P13 D6) or a person. A vision rung is opt-in, behind the egress policy (ADR-P26) and the budget (ADR-P14).
5. **D5, one intake Task per document.** An agent that accepts files declares `doc.intake` as a child Task keyed by doc key. A Task that declares no extraction ends at context only, with no model call. Classification of the kind is its `DecideStep` under an agent-declared alias (bound per D12), with options read from the registry projection (ADR-P07 S1 `options_from`), which always lists `unknown_kind`; the registry snapshot is a lineage input, and bands not fitted on that label set are unfitted. Extraction is its `ExtractStep` over a recorded `read_document` rendering, chunked on anchors when the text exceeds the extractor's input cap, with conflicting chunk values abstaining. The Task has one interrupt (ADR-P15), covering kind and fields, with expiry, cancel and decline edges.
6. **D6, one kind for all extractions.** Results save under `doc.extraction`, content `{schema_ref, fields}`. Its schema check reads the pinned `doc.schema` version and its cite policies are that entry's per-field policies, so ADR-P04 D7's save path (citations, phrase rules, verifiers) runs unchanged and no run-time kind reaches the artifact store.
7. **D7, every path ends in something the model is told.** A gapped document that a Task must extract saves every required field as `Unknown(reason="unverified")` with a question. Model unavailability and schema-invalid output give `Unknown(reason="not_checked")`. A quote that fails the verbatim check is retried once, then demoted. Nothing is dropped silently.
8. **D8, a schema registry with proposal as a separate Task.** `unknown_kind` accepted with confidence starts `doc.propose_schema`, run by a generation alias; a System 2 model never answers in place of a System 1 one. The draft must pass a meta-schema check (JSON Schema, field types supported by the bound extraction adapter, per-field cite policy, no name collision), then a trial by the extraction model on the seed document, gated at a located share recorded in parts per million. The seed trial is necessary, not sufficient (`seed_only` and, in setup 1, `same_model` are recorded). Decided 26 Nov 2026 (ING-3): a proposed schema is used only after review by default; an agent may opt in (`schema_draft_use=True`) to use a draft within the current run, and no draft is reused in another run without review. Proposals are serialised per owner by a lease, edited approvals are re-checked, a rejected draft is a source change that invalidates extractions pinned to it, and a draft-schema extraction never gates an external effect.
9. **D9, rendering is a read, bounded and recorded, by route.** Documents render in ADR-P08's rendered-reads section, never in the request. Code picks a route per consuming step: native when the bound model accepts the detected type and the context policy allows it (images by default, other types by opt-in), the text representation otherwise, and the header alone with a named gap when an image meets a model with no image input. The route is `Rendered.mode` on `step.started`; the text representation exists whatever the route. Each document renders as a header built by code from the records, then the full text up to the per-artifact read cap or the `doc.representation` summary template above it. When the read block would overflow, older or larger documents drop to summary and then to header only, recorded in `Rendered`. `read_document` declares `mints_citation_spans`, is capped per call, and its renderings feed `Rendered.spans`, so ADR-P13 S1's `shown_to` holds. The header escapes the file name, prints only lines whose facts exist, and applies the data rule to uploads and tool results, not to a user's own long message.
10. **D10, oversized inputs become documents with a reference.** A text tool result at or under ADR-P08's `result_cap_chars` is unchanged. One over the cap is also stored as a one-version document on the same ADR-P04 D10 blob; the step's view keeps ADR-P08 D4's head and tail plus the reference, and `read_document` pages it and mints spans over it. A user message over a placeholder size is stored the same way and keeps its instruction authority. A file, binary or image tool result enters intake like an upload.
11. **D11, replay never re-converts.** Converter pins and quality signals are in the representation's content hash and in `canon_exclude`, so a converter upgrade that yields the same text stops invalidation at the representation (ADR-P05 early cutoff). Replay (ADR-P22) and recovery read the recorded representation, because OCR and vision rungs are not deterministic. A sent image copy is recorded by hash in `Rendered`, so a replay sends the recorded bytes.
12. **D12, routing is configuration.** System 1 models are optional. Each alias resolves to its configured System 1 model; with none configured, `classify.doc_kind` and the extraction alias resolve to the generation alias in adapted mode, recorded in `run.started`, and there is no routing step (setup 1). In setups 2 and 3, adapted is a cascade rung after the native ones and needs `allow_adapted`. Adapted extraction is allowed (decided 26 Nov 2026, ING-2, an ADR-P07 amendment): code checks the quote verbatim inside an already-minted span, the answer is `native=False`, it never gates an external effect, and the loader requires a verifier on exact-value fields an adapted extractor can fill. A code verifier satisfies that rule, so setup 1 needs no System 1 key; a field with none is refused at load (`verifier_missing`) unless its cite policy routes it to a person.
13. **D13, image preparation is code.** At intake: sniff, a decoded-pixel cap, decode (pillow-heif for HEIC), EXIF orientation applied, sRGB, metadata stripped, animated GIF to its first frame, multi-page TIFF split into pages. Per target model: resize with the provider's own formula from its image profile (28 px patches with a per-model token budget; 32 px patches times a multiplier; or a flat cost per image), pass through PNG, JPEG and WebP that already fit, encode once from the original, cache by (hash, profile), and estimate tokens from the formula. Before each call, images are counted across the whole request, and over a profile's many-image threshold older images are re-derived smaller or replaced by sidecar plus reference. History holds references, never inline base64. No model takes part, so preparation is identical in every setup.

Worked example, toy numbers. A 14-page PDF arrives. Pages 1 to 6 and 11 to 14 pass the fast rung at about 2,800 characters each; pages 7 to 10 give under 20 characters and no OCR rung is declared. The representation records ten `extracted` pages and drops pages 7 to 10. The header says "pages 7-10: scanned, not read". The kind classifier answers `pricing_page` at 910000 ppm, over `accept_at` 800000. Extraction locates five of seven fields with verbatim quotes; the two whose values sit on pages 7 to 10 save as explicit unknowns with questions. In setup 1 the same run has no decision model: the general model answers `pricing_page` in adapted mode under its own thresholds, fills the fields with quotes that code checks verbatim, and the exact-value price fields pass only because the kind declares a code verifier (line items sum to the total).

## Alternatives considered

| Option | What it is | For | Against | Verdict |
|---|---|---|---|---|
| A. Native pass-through only | Send each file to the model as a content block; replace an unsupported one with a note | No conversion code | No text to verify citations against; provider conversion is hidden and differs; nothing says what was dropped | Rejected; kept as a per-consumption rendering mode, designed only |
| B. Ordered middleware stages | Agents and extensions register functions before and after ingest and the model | Familiar, flexible | Order becomes an unchosen precedence; a hook can edit a model request (ADR-P20 Alternative A) | Rejected |
| C. Per-document rung choice | Judge quality once per document | Simpler | A mixed PDF either hides scanned pages behind an average or re-parses every page at a costlier rung | Rejected for per-page |
| D. General model answers when no schema fits | Generation fills the decide or extract contract directly | One call | Not thresholdable; breaks the System 1 contract | Rejected; proposal is a separate Task whose output System 1 checks |
| E. Run-time artifact kinds per proposed schema | Each approved schema becomes a declared kind | Natural typing | The loader never sees them; cite policies and save checks have nothing to read | Rejected for D6 |
| F. Large text tool results become documents | Route every oversized result through ingestion | One path; one reading tool | Risk of a second copy, and an immutable step output as a versioned source | Chosen in a narrowed form (D10): over the cap only, one version, the same blob |
| H. Require a System 1 decision model | Every document passes a decision model first | One code path | Users with one model key cannot extract at all | Rejected for D12 |
| I. Send images unchanged | Bytes to the provider; it resizes | No code | Stored image differs from what the model saw; a provider's many-image and tool-result rejections; EXIF leaks; flat token estimates | Rejected for D13 |
| J. Let a model choose image size or format | A router or the general model picks resolution | Adapts to content | A model in the loop for arithmetic; differs by setup; not replayable | Rejected for D13 |
| G. Extension with declared contributions | D1 to D13 | Every step recorded and inspectable; no new core mechanism | Three kinds, two Tasks, two tools and a profile table to maintain | Chosen (ING-5, 26 Nov 2026) |

Related work, for the reader: model cascades with deferral (FrugalGPT, Chen, Zaharia and Zou, 2023; AutoMix, Madaan et al., 2023) for the abstain-then-escalate shape; JSON Schema (2020-12) for the registry; the W3C Web Annotation Data Model (2017) for the quote and position selectors on spans; RFC 6838 for media types, RFC 5322 for email and RFC 4648 for the base64 an upload's binary files travel in.

## Interface

```python
# aksha/ext/ingest/types.py (proposal; design only)
Origin = Literal["upload", "message", "tool_result", "attachment"]
Rung = Literal["parse_fast", "parse_layout", "parse_vision"]
Trust = Literal["extracted", "engine_written", "model_written"]
GapReason = Literal["refused", "locked", "unsupported", "unreadable", "cancelled"]
Setup = Literal["s2_only", "s1_decide", "s1_decide_extract"]
RouteKind = Literal["text", "native_file", "native_image", "header_only"]

@dataclass(frozen=True)
class ConverterSpec:                 # declared data; the extension's ladder runner calls it
    media_types: frozenset[str]
    rung: Rung
    trust: Trust
    min_chars_per_page: int
    max_garbled_ppm: int
    max_pages: int
    max_stream_bytes: int
    time_cap_ms: int
    memory_cap_bytes: int | None     # enforced only once rungs run in a subprocess

@dataclass(frozen=True)
class Document:                      # a record of the "documents" versioned source
    doc_key: str
    version: int
    blob: str                        # "blob:sha256:<hash>"
    name_as_given: str
    origin: Origin
    parent: str | None               # email doc key for an attachment, step ID for a tool result
    declared_type: str | None
    detected_type: str
    charset: str | None
    type_conflict: str | None
    size_bytes: int

@dataclass(frozen=True)
class Gap:
    doc_key: str | None              # None when refused before storing
    reason: GapReason
    detail: str                      # "empty", "decompression_cap", "OCR not available"
    steps: tuple[str, ...]           # rung step IDs tried

@dataclass(frozen=True)
class Representation:                # content of kind "doc.representation"
    document: tuple[str, int]
    converters: tuple[str, ...]      # in content_hash; in canon_exclude
    text_blob: str
    anchors: tuple["Anchor", ...]
    pages: tuple[tuple[str, Rung, Trust], ...]
    dropped: tuple[str, ...]
    signals: tuple[tuple[str, int], ...]   # in canon_exclude
    segments: tuple = ()             # reserved for multi-kind documents
    image: "ImageInfo | None" = None # source size after orientation, frames, metadata stripped

@dataclass(frozen=True)
class ImageProfile:                  # per bound model; the extension's table until ADR-P07 is amended
    profile_id: str
    accepts: frozenset[str]
    cost: Literal["patch", "flat"]
    patch_px: int | None             # 28 or 32
    max_long_edge: int
    max_patches: int | None
    reject_over_patches: int | None
    token_multiplier_ppm: int
    flat_tokens: int | None          # a flat-cost provider
    many_image_threshold: int | None # counted across the whole request
    many_image_max_edge: int | None  # read from config; it has changed over time
    max_bytes_per_image: int
    resize_in_tool_results: bool
    ref_kinds: frozenset[str]

@dataclass(frozen=True)
class SentImage:                     # cached by (source_sha256, profile_id)
    source_sha256: str
    profile_id: str
    sent_wh: tuple[int, int]
    scale_ppm: int
    media_type: str
    passed_through: bool
    blob: str
    est_tokens: int
    formula: str

@dataclass(frozen=True)
class Route:
    kind: RouteKind
    sent: SentImage | None
    reason: str | None               # "no vision-capable model configured"

def ingest(owner: str, name: str, data: bytes, origin: Origin,
           *, doc_key: str | None = None) -> Document | Gap: ...
def normalise(doc: Document, ladder: Sequence[ConverterSpec]) -> Representation | Gap: ...
def render_header(doc: Document, rep: Representation | None, gap: Gap | None,
                  shown: "Rendered") -> str: ...                               # pure
def prepare_image(doc: Document, profile: ImageProfile) -> SentImage: ...        # pure over bytes
def plan_request_images(blocks: Sequence[SentImage], profile: ImageProfile) -> Sequence["ImagePlan"]: ...
def resolve_route(setup: Setup, bound_model: str, policy: "ContextPolicy", doc: Document) -> Route: ...
def offload_result(step_id: str, text: str) -> Document: ...                    # same blob, one version

# Events: document.stored{...}, document.gap{doc_key, reason, detail, steps},
#         document.refused{cap, value, origin}; rungs and trials are step.started / step.completed;
#         a sent image copy is recorded by hash in Rendered on step.started
# Command: AttachDocument(session, data, name, doc_key=None, lane=None)  # lane required on an open run (ADR-P27)
```

## Amendments this needs elsewhere

| ADR | Amendment |
|---|---|
| ADR-P08 D4, S1 | A text tool result over `result_cap_chars` is also stored as a one-version document with a reference; `read_document` renderings are a route to `Rendered.spans`; `Rendered.mode` gains `"native"` and the sent image hash; images count against the caps by their token estimate |
| ADR-P13 S1, D4 | `shown_to` names `read_document` renderings; `CitePolicy` default excludes non-`extracted` spans for exact-value fields |
| ADR-P07 | Decided (ING-2): an adapted extractor may fill extraction when code checks its quote verbatim inside an already-minted span, `native=False`, never gating an effect, with a verifier required at load on exact-value fields. Also: an alias with no System 1 binding resolves to the generation alias in adapted mode. Open (ING-9): whether `ModelCapabilities` gains native-input and image-profile fields; until then they live in the extension |
| ADR-P08 D3 | Open: a user message over a placeholder size becomes a pageable document, the one case where the request is reduced |
| ADR-P19 | One command, `AttachDocument` |

## First milestone

Ingestion as a library with a test harness, because the artifact save path, context assembly, span minting, the session API and the loop are not built. The maintainer widened the format scope on 26 Nov 2026. The build is ordered so that stopping after any step leaves a tested set:

1. A SQLite blob table.
2. A sniffer for every type below, with the allow-list and ZIP caps on the detected type.
3. Plain text, Markdown, JSON and CSV (standard library).
4. PDF through pypdf: text layer, `[p.N]` anchors, scanned pages as a named gap, encrypted PDFs as `locked`.
5. HTML (standard library).
6. DOCX, PPTX and XLSX through python-docx, python-pptx and openpyxl, with heading, slide and sheet-range anchors.
7. Email (`.eml`) with attachments as child documents.
8. Images at intake: Pillow with pillow-heif 1.9.0 or later, pixel cap, EXIF orientation, sRGB, metadata stripped, GIF first frame, TIFF pages, original kept.
9. Images per target model: four image profiles, the provider formulas, pass-through, the sent-copy cache, and request-wide image counting.
10. Route resolution for the three setups and the native image route, with the header-only gap when no vision-capable model is configured.
11. Oversized tool results and user messages stored as documents with a reference.
12. The representation saved, the header renderer, and the three `document.*` events.
13. Offline tests for each of the above.

The parser and image libraries are optional dependencies behind `aksha[ingest]`. OCR, vision descriptions, native PDF and Office pass-through, live model calls, `read_document` spans and `AttachDocument` are designed only. My estimate is several focused days, not one, so a dated cut takes the steps in order and claims only what passed.

## Amendments from the build (26 Nov 2026)

What was built, each tied to the tests that check it (`tests/unit/extensions/ingest/`, `tests/integration/test_ingest_uploads.py`, `tests/integration/test_http.py`):

| Step | Built | Tests |
|---|---|---|
| 1. Blob storage | The existing blob store (`BlobStore` port, `adapters/blobs/filesystem.py`, decision 25) holds every original and every sent image copy by `sha256:` hash. No separate SQLite `blobs` table: the store that decision 25 built already is content-addressed, write-once and re-hashed on read | `test_an_image_profile_prepares_a_sent_copy` |
| 2. Sniffer | `sniff.py`: magic bytes, ZIP part names, OLE (encrypted Office is `locked`, legacy Office a named gap), image magic with the HEIC and HEIF `ftyp` brands, HTML and SVG roots, strict JSON, RFC 5322 headers; charset by BOM, strict UTF-8, then the declared fallback; the allow-list and the ZIP size and entry caps on the detected type | `test_sniff.py` |
| 3. Text family | `text.py`: plain text, Markdown heading anchors, JSON pretty-printed with an anchor per top-level key, CSV with the delimiter sniffed and `rows 1-200` anchors | `test_formats.py` group 3 and 4 |
| 4. PDF | `pdf.py`: pypdf 7.1.1, `p.N` anchors, characters per page and replacement-character share per page, scanned pages dropped and named, an all-scanned PDF `unreadable: ... OCR not available`, an encrypted one `locked`, the page cap and pypdf's decoded-stream limits as `decompression_cap` | `test_a_quote_sits_at_the_recorded_page_3_anchor` and the four PDF tests after it |
| 5. HTML | `text.html_doc`: `script`, `style`, `noscript` and `template` dropped and counted, headings as anchors, table cells as pipe rows | `test_html_drops_script_and_style_and_anchors_headings` |
| 6. Office | `office.py`: DOCX heading anchors and table rows, PPTX `slide N` anchors with speaker notes, XLSX read-only with cached values (no formula evaluated), `Sheet!A1:C200` anchors, a per-sheet cell cap | `test_formats.py` group 3 |
| 7. Email | `mail.py`: headers, the plain body or the HTML body as visible text, attachments as child documents (`attachment`, `parent` set) under the depth and per-email caps, refused by name past them | `test_an_email_with_a_pdf_attachment_is_two_documents`, `test_a_nested_email_past_the_depth_cap_is_refused_by_name` |
| 8. Images at intake | `images.py`: the pixel cap from the header before decoding (and Pillow's own guard as a refusal), `exif_transpose`, sRGB, metadata never carried over, GIF frame 0 with the rest named, multi-page TIFF as `p.N` pages, the original untouched; a sidecar that says "not described" | `test_images.py` group 7 |
| 9. Images per model | `profiles.py`: four profiles named for their arithmetic (`patch28-standard`, `patch28-hires`, `patch32-high`, `flat-1120`), each provider's own resize rule, pass-through byte for byte when an image fits, one encode from the canonical pixels, the sent-copy cache, token estimates from the formula, `plan_request_images` across a whole request | `test_each_provider_formula` (1920x1080 to 1456x819 at 1,560 tokens; 2048x2048 to 1600x1600 at 3,000; a flat 1,120), `test_twenty_three_images_to_a_patch28_profile_none_over_the_limit` |
| 10. Routing | `routing.py`: `resolve_bindings` for the three setups (setup 1: `decide` and `extract` resolve to the general model, adapted, `routed=False`, no routing step), `resolve_route` (an image native to a vision-capable profile, else header only with "no vision-capable model configured"; every other type text) | `test_routing.py` |
| 11. Oversized inputs | `offload.py`: `offload_result` (one version, the same `sha256:` ref as the blob store gives the full output, head and tail view with the reference) and `offload_message` (instruction authority kept, no data rule) | `test_offload_and_headers.py` group 11 |
| 12. Representation saved | The representation is what the input workspace serves at the input's path: the header, a blank line, then the text. Not yet a `doc.representation` artifact: no ingest artifact kinds are declared | `test_run_with_binary_inputs_converts_them_and_tells_the_model` |
| 13. Header | `header.py`, with snapshots for an upload with dropped pages, a gap, a native image, an image with no vision model, a long message and an oversized tool result; the file name escaped as a JSON string, control characters removed, capped at 120 characters | `test_offload_and_headers.py` group 12 |
| 14. Events | `document.stored`, `document.gap` and `document.refused` in the core catalogue, writer `inputs`, boundary U8, committed with the upload's source change; no rung steps are recorded yet (intake runs inside the upload command, not as durable steps) | `test_run_with_binary_inputs_converts_them_and_tells_the_model` |

Where the build differs from the text above:

1. **Wired into uploads, beyond the library milestone.** `UploadInputs` gains `binary` (path to base64, RFC 4648); `aksha run --input` and `aksha upload` send every file's bytes, UTF-8 text in `files` and the rest in `binary`, and print one line per document. The runtime calls the extension through a port (`runtime/inputs/ports.py`, `DocumentIngester`), built in the composition root (`aksha/uploads.py`); a runtime with no ingester takes UTF-8 text only, as before.
2. **The input path stands in for the doc key the client passes (D2).** `UploadInputs` replaces by path, so a path uploaded again keeps its doc key and moves its version only when its bytes moved; the version is in the header, so the record's version moves with it and ADR-P05 reopens what read it. A nameless replacement (`AttachDocument`) is still designed only.
3. **UTF-8 text an agent's tools parse is served as it is.** Plain text, Markdown, CSV, JSON, HTML, SVG and an email with no attachments, decoded as UTF-8 with no type conflict, keep their bytes, so the shipped agents' parsers and citation offsets are unchanged; only `document.stored` records them. Everything converted (PDF, Office, an email with attachments, images, text in another charset, a type conflict) is served as header plus representation.
4. **pillow-heif is not in any extra.** Checked on 26 Nov 2026: its binary wheels are GPL-2.0 as a whole (risk 5). HEIC decodes when a user installs it; otherwise it is a gap that says so.
5. **Image profiles are named for their arithmetic, not their provider**, and chosen by `AKSHA_IMAGE_PROFILE`; unset means no vision-capable model. The sent copy is prepared, stored and recorded on `document.stored{route}`; the native image block itself is sent once the agent loop's provider adapters exist.
6. **An invalidation fix the build needed (ADR-P05 S2).** A source never recorded before is now also recorded when a change applied by a command (an upload) gives one of the owner's recorded queries a new result-set hash, not only when a read record moved; recovery's check is unchanged. Before, a changed file no work had read (a converted PDF that a recorded search would now hit) reopened nothing on the first change. `test_a_changed_pdf_keeps_its_key_moves_its_version_and_reopens_the_work_on_it` proves the path end to end.
7. **A converter that raises is a failed rung.** The document becomes a gap `unreadable` naming the error and the upload goes on (`test_a_converter_that_raises_is_a_gap_not_a_crash`). The type check and the sent-copy step are guarded the same way: a sniffer that raises is a gap `unreadable`, a sent copy that fails leaves the image header only. A UTF-8 text file whose converter failed is still served as it is, with its `document.gap` recorded.
8. **Caps added in review (`test_hostile_inputs.py`).** The decoded-pixel cap is a budget across every kept frame, and each TIFF page's own size is checked before it is decoded (Pillow checks only the first); pages past it are dropped and named. A blank spreadsheet row counts as one cell, and `Caps.max_book_cells` bounds the whole workbook, so a sheet that declares a million blank rows cannot loop for long. Anchor labels are deduplicated in linear time. JSON nested past the parser's depth is text. Every file-controlled string in a `document.*` fact (a name, a gap's detail, a dropped line) is cut to 512 characters and the dropped list to 32 entries, so no attachment name can push a fact past the payload cap. An upload that names an attachment's served path itself is refused.
9. **Fixes from the pull request review.** An uploaded file owns its attachment folder: when an email is uploaded again, any `<path>.parts/` file the new upload no longer serves is removed from the inputs workspace (`InputWorkspace.remove`), and the source change records it as gone (`test_an_email_uploaded_again_without_an_attachment_removes_the_old_part`). The next doc key counts past any key named on the first line of a served file, as well as past the log. So an upload cut off after its files were written, and before its facts committed, never has its key handed to another file (`test_an_upload_cut_off_before_its_facts_does_not_hand_its_doc_key_out_again`). The sent-copy cache, which lives as long as its session, is bounded at 256 copies and 64 MiB. The least recently used copy goes first, and a dropped copy is derived again on its next use (`test_the_sent_copy_cache_is_bounded_by_entries_and_bytes`).


## Proposed amendment (27 Nov 2026): every parser in a worker process

Proposed, built in `aksha.extensions.ingest.worker` and tested in `tests/unit/extensions/ingest/test_parser_worker.py`; it replaces risk 1's accepted risk and narrows risk 4. The first milestone ran the sniffer and every converter inside the runtime's process with no time cap, so a file that made a parser spin, allocate without bound or crash could hang or kill the runtime. Process isolation with resource limits is the standard answer (POSIX `setrlimit`; Linux user and network namespaces, `unshare(2)`), and the interface above already reserved `time_cap_ms` and `memory_cap_bytes` for it.

1. **One function crosses the boundary.** The type check and the converter are `parse_in_process` (`parse.py`); `ingest` takes `parse`, and `UploadIngester` passes a `ParserPool` by default, so every upload, and every email attachment, is parsed in a worker. The allow-list, the doc keys, the header and the facts stay in the host. A library caller that passes nothing parses in process, as before.
2. **Hard limits.** The worker sets its own limits before it reads input: address space (`RLIMIT_AS`) to `Caps.parser_memory_bytes` (2 GiB by default), CPU time per job (`RLIMIT_CPU`) to its use so far plus `Caps.parser_cpu_s` (60 s), no core files, no growing a file (`RLIMIT_FSIZE` 0) and no new processes (`RLIMIT_NPROC` 0). Neither limit is enough alone: `RLIMIT_FSIZE` does not stop truncating or deleting a file, and `RLIMIT_NPROC` does not bind root or a process holding `CAP_SYS_ADMIN` or `CAP_SYS_RESOURCE` (getrlimit(2)). So the worker also restricts itself with Landlock (Linux, unprivileged, `landlock(7)`): from ABI 3 no write, truncate, delete, create, rename or link anywhere, from ABI 4 no TCP bind or connect, and from ABI 6 no abstract Unix socket or signal outside itself. Then it tries to fork. Its ready frame says whether its writes are contained and whether it could fork, and the pool refuses a worker whose writes are not contained (no Landlock ABI 3 and no bubblewrap box) or that could still fork: the document is a gap `parser_uncontained` and no parser runs. The host holds the wall clock, `Caps.parser_time_ms` (60 s): past it the worker's whole process group is killed with `SIGKILL`, since the worker starts its own session. Its environment is `PATH` and `LANG` only, so no key reaches it.
3. **No network.** The worker enters a new user and network namespace before it reads input; its only interface is a loopback that is down. A kernel that refuses unprivileged namespaces leaves the worker without one, and `ParserPool.isolation` says so.
4. **Named gaps.** A parse that misses the wall clock or the CPU cap is a gap `unreadable` whose detail starts `parser_timeout`; one that runs out of memory, `parser_memory` (a `MemoryError` is never read as a damaged file: the converters re-raise it); one that dies any other way, or replies outside the protocol, `parser_crashed`; one the pool refused because it could not be contained (item 2), `parser_uncontained`. The worker reports the type check before it converts, so a converter that fails still leaves the detected type on the document. The upload goes on.
5. **The reply is data.** The worker replies in JSON frames with raw byte blobs (`wire.py`), checked field by field against the closed sets above, never unpickled, under a size cap (256 MiB plus four bytes per capped pixel). Images cross as raw pixels rebuilt with `Image.frombytes`, so no image format is parsed in the host. A worker a hostile file took over can at worst make its own document's text or gap wrong.
6. **A small pool.** Up to two idle workers are kept per set of limits and reused for up to 50 jobs, so the interpreter and the parser libraries load once; at most four parse at once. A worker that hit a limit, died or replied badly is discarded, never reused.
7. **Inside bubblewrap when configured.** When `AKSHA_SANDBOX` names the bubblewrap backend (`bwrap`, `bwrap:os-jail` or `auto`) and it probes available (ADR-P12), each parse runs in its own box: every namespace unshared, the allowlisted system root and the interpreter's files read-only, and the one input file read-only at `/input/document`. A jailed worker holds one input, so it is never pooled; each document then pays an interpreter start (about 0.7 to 1.3 s on the development machine, 27 Nov 2026).

Not covered: UDP and other non-TCP sockets where the kernel refuses unprivileged namespaces (Landlock ABI 4 to 8 handles TCP only); a cgroup memory limit (`RLIMIT_AS` bounds the address space, which is stricter than resident memory and needs no privileges); seccomp filtering of system calls inside the worker; the sent-copy step, which resizes and encodes already-decoded pixels in the host (no file format is parsed there).

Still designed only: OCR and vision rungs (so image descriptions); the native image call; native pass-through of PDF and Office files (ING-6); `ImageProfile` inside ADR-P07 (ING-9); SVG rasterising; per-rung caps and rungs as durable steps (one worker limit covers the type check and the fast rung together, see the proposed amendment); legacy `.doc`, `.xls`, `.ppt` and `.msg`; audio and video; main-text extraction for HTML; `AttachDocument`; `read_document` spans and the header's `more:` line; live decide and extract (ADR-P07 row 8), with `resolve_bindings` called by the model router (today only its tests call it) and the adapted adapter with its `verifier_missing` check (ING-10); schema proposal and `schema_draft_use`; the converter cache; multi-kind documents; erasure (ADR-P30); threshold fitting; the offload of oversized tool results and user messages in the agent loop (the library functions exist and are tested; nothing calls them yet).

## Stress test (26 Nov 2026)

Verdicts against the design above, by my reading:

| Agent | Verdict | Scenario |
|---|---|---|
| Research | Fits | A vendor's pricing PDF with four scanned pages: text pages extracted, scanned pages named in the header, the prices on them saved as explicit unknowns |
| Patent | Fits with agent-side code | A prior-art PDF read by the intake Task; until fast-class aliases are built, kind and fields are rule code in the agent's tools |
| Coding | Fits | A log file attached as text: typed by charset, rendered under the read cap, paged with `read_document`; no extraction declared, so context only. A 120,000-character test log returned by a tool becomes a one-version document with a reference |
| Any agent, setup 1 | Fits | One general-model key, no System 1 models: every request goes straight to the general model, which fills classification and extraction in adapted mode; exact fields pass only through declared code verifiers or a person |
| Coding, screenshots | Fits | A browser-use tool returns 30 screenshots over a session: each is resized to the bound model's profile on entry, and past 20 images in the request the older ones go out smaller or as sidecar plus reference, so no request is rejected |

## Risks

1. A malicious file can still exploit a parser, but since the proposed amendment of 27 Nov 2026 only inside a worker process with a wall-clock, CPU and memory cap, no network, no file writes and no new processes, and inside a bubblewrap box with read-only access to its one input when that sandbox is configured. What a worker can still do is make its own document's representation wrong, within the reply's checks; and with neither bubblewrap nor unprivileged namespaces on the host, the worker has the limits and Landlock's TCP block but no other network isolation. A host with neither bubblewrap nor Landlock ABI 3, or one where the runtime runs as root without bubblewrap, gets the gap `parser_uncontained` for every document rather than an unconfined parser.
2. Uploaded files are the likeliest carrier of personal data, and blobs are insert-only. Erasure is open in ADR-P30; this design only adds tombstone checks before blob reuse.
3. Every threshold here is a placeholder and unfitted; the run inspector lists them.
4. Image decoders (Pillow, pillow-heif) parse untrusted bytes in the parser worker (risk 1), never in the runtime's process. The pixel cap and current pillow-heif (never `pi-heif`, whose bundled libheif has a known heap overflow) narrow what a decoder bug can reach.
5. Licences of the codecs bundled in pillow-heif's wheels. Checked on 26 Nov 2026 against pillow-heif 1.9.0's `LICENSES_bundled.txt`: libheif and libde265 are LGPL-3.0, the bundled x265 encoder is GPL-2.0, and the file states the binary wheels as a whole are GPL-2.0. So no extra of this package installs it; HEIC decodes only when a user installs pillow-heif themselves, and is otherwise a named gap.
6. Provider image limits change: one provider's many-image edge was 2,000 px in Feb 2026 and 3,000 px per its documentation on 26 Nov 2026. Profiles are configuration and dated; where a provider offers a setting that turns a silent resize into an error, coordinate-sensitive images use it, so drift shows as an error.
7. The widened first build does not fit one day; the ordered steps limit the damage of a cut.
