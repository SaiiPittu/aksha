# ADR-P13: Grounding and citations (tools mint citation spans, the runtime core verifies every quote at save, and a claim it cannot ground becomes an explicit unknown)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026, with one part Accepted. The maintainer's decision 6 in the decisions log puts citation validation in the runtime's artifact store, so that every agent's saves get it, and requires excerpt references on extracted artifacts; decision 9 requires every typed relation to carry its own excerpt as evidence. Those two are Accepted. The mechanism below (tool-minted spans, verbatim verification, the explicit-unknown type, the generalisation to any versioned source) is not recorded as the maintainer's call and stays Proposed, as v2's ADR-006 does. Amended 25 Nov 2026 after the three-agent stress test: S1 to S3 below are new and Proposed.

## Terms

Primitive PR-13 in the primitives catalogue, cluster C (acting safely). Depends on PR-04 (citations bind to artifact and source versions; lineage records what was read) and PR-10 (which tools may mint spans). Needed by PR-09 (memory records carry their sources).

Terms used here, in the industry names of the design glossary:

- **Grounding** means every claim in an output points to evidence a tool returned in this run.
- **Citation span** (v2's "excerpt") is a passage a read or fetch tool returned, minted by the runtime core into an ID such as `EX-0042`, bound to the tenant and to the source's version. Its shape is a **quote selector**: The W3C Web Annotation Data Model's `TextQuoteSelector` (the exact text) plus a `TextPositionSelector` (character offsets).
- **Citation** is `{span, quote}` in saved content; the quote must appear verbatim in the span.
- **Citation verification** (v2's "citation binding") is the save-time check that a citation's span exists, was shown to this Task instance, is at the source's current version, and contains the quote verbatim.
- **Explicit unknown** (v2's "named gap") is a field the agent could not ground, stored as `Unknown` with a question and a reason, never as zero, an empty string or a guess.
- **User statement** is a person's answer to an interrupt, minted as a citable span of its own.

## Context: a citation the runtime cannot check is the model's prose, and prose can cite anything

The product requirements are specific: search snippets alone do not support a claim; each finding links to the exact supporting excerpt; identifiers and dates are recorded exactly as the source states them; missing data stays unknown, not zero, and a failed fetch stays unverified; recommendations link to sources the tools actually returned. The check a reviewer can run on that requirement is mechanical: did a tool return this text?

Three observations shape the design:

1. Without a citation type, citation is left to the model's prose, whose nearest form is a file path and line number nobody checks (catalogue, PR-13).
2. Carrying spans is not enough if only bounds are checked. An extraction can attach a source with text offsets per value and validate that the offsets are in range, without checking that the text says what the value claims.
3. Research on attribution says why the check must be the runtime's. Rashkin and others define attribution as output checkable against an identified source that was provided; Liu, Zhang and Liang (2023) found many statements from generative search engines unsupported by their own citations (both read as abstracts, the quote sheet in the design notes).

What breaks without this primitive: The Research Agent saves a price cell citing a snippet it never fetched; the Patent Agent records a publication date paraphrased from memory; the coding agent reports "all tests pass" with no test run behind it. Each looks grounded and none is.

## Decision

1. **Only tools mint citation spans, and only tools that declare `mints_citation_spans`** (ADR-P10). A tool body returns span drafts (source, version, offsets); the runtime core checks each draft against a record the body's reader actually returned in that attempt, then mints the span with its full text into the event store. Search results carry no span and can never be cited. A model cannot mint a span.
2. **Every save verifies every citation, and no extension can switch it off** (decision 6, FR-TD-15). `verify_citation` runs in the artifact store's save path for content and for every typed relation's evidence. In order: The span exists for this tenant (`unknown_span`); it was returned to or rendered for this Task instance (`span_not_seen`, AE-7 lean B, so a citation proves the text was in front of the model); its source version is current (`stale_source`); the quote, with whitespace runs collapsed and nothing else normalised, occurs in the span text (`quote_not_verbatim`, with the span's text in the error so a small model can copy it); and the quote is at least 12 characters or the whole span (`quote_too_short`). A failure refuses the save with the failing path named; the model corrects and saves again.
3. **The span shape follows the W3C selector, so the source can be anything versioned.** A span records `source`, `record_id`, `version`, a quote selector and a position selector. The runtime core does not care whether the source is a fetched page, a fixture record, a person's statement, an artifact version, a file in a workspace tree at a tree hash, or the stdout of an exec. This is the generalisation v2 lacked: its excerpts were defined against fixture record versions only.
4. **Three cite forms, one verifier.** A field cites a span (`{span, quote}`), a user statement (`{statement}`), or an artifact path at a pinned version (`{artifact, path}`). Which form each field may use is a per-field cite policy the agent declares on its artifact kind (a matrix cell may not cite a user statement; only the business-importance factor may cite a Research artifact). The policy is data; one function enforces it.
5. **An explicit unknown is a core value type, not a convention.** A field declared `may_be_unknown` may hold `Unknown(question, reason, checked)` instead of a value. The schema validator accepts it only on those fields, and a save that puts `0`, `""` or `null` into a numeric or cited field that has no valid citation is refused as `ungrounded_value`. The reason is one of `not_found` (every listed record was read in full at its current version), `unverified` (a step failed or a hit was never fetched, named) or `not_checked`. Richer status sets, such as the evidence-grid statuses `conflict` and `supported` (05b section 8.3), are an extension built on top.
6. **A claim the fast model cannot locate becomes an explicit unknown, not a refused save (designed).** When an artifact kind declares a verifier on a field, the save path asks an extraction or classification model to locate the claimed value inside the cited quote (HLD 9.5). Verbatim verification is code and always runs first; the model can only demote a claim to `Unknown(reason="not_located")` with a question, never promote one. This ships with model routing (PR-07, cut row 8).
7. **Model-returned citations are hints, re-verified.** A provider that returns citations with character ranges is adapted by the model interface into span-and-quote pairs over spans the runtime already minted; they pass the same verifier. A provider citation to text no tool returned fails `unknown_span`.
8. **Span text lives in the event store in the first milestone.** `excerpts.minted` (v2's event name, until the event-store ADR) carries the passage text, so an old quote can be re-verified and shown in a review diff. Stored input artifacts for every read result are the general design (RQ-8).

### Amendments from the stress test (25 Nov 2026, Proposed)

1. S1, `shown_to` covers only text the model was shown. A tool that mints a span over a pricing table hidden by truncation still set `shown_to` for that span, so the model, having seen the price in a search snippet, could cite the hidden span and pass verification. Nothing false is saved, but D2's promise that a citation proves the text was in front of the model fails. `shown_to` is now the union of two sets: spans whose text lies inside the view `truncate_result` produced (ADR-P08 D4), and `Rendered.spans` of the instance's renderings (ADR-P08 S1). A span outside both becomes citable once a later read renders it.
2. S2, `stale_source` compares the record, not the source. D2's "source version is current" was ambiguous. Read at source grain, the Patent change set (one contributor record added) refuses a re-save of C2's extraction that cites unchanged record CON-07, which would turn "preserve unaffected work" into "unaffected work cannot be saved again". The check uses `record_version(owner, record_id)`, and for a span over a tree file, the file's entry hash (ADR-P04 S3).

   ```python
   # aksha/runtime/grounding.py (proposal)
   def verify_citation(cite: "CiteForm", spans: "SpanIndex", instance: str,
                       sources: Mapping[str, "VersionedSource"], owner: str,
                       policy: "CitePolicy") -> "Verified | CiteError":
       """stale_source when sources[span.source].record_version(owner, span.record_id)
       differs from span.version, or is None (the record is gone)."""
   ```

3. S3, an optional `value_in_quote` flag on `CitePolicy`. The Patent agent's requirements wants publication IDs and dates "exactly as the source states them". The first milestone's verifier checks the quote is in the span, not that the stored value is in the quote. `CitePolicy.value_in_quote: bool = False` makes the second check generic; until it ships, the Patent Agent's `rules.validate_form` asserts it.

Agent-side, no core change: The Research Agent's `claim_status` (`fact`, `vendor_claim`, `inference`, `unknown`) is agent schema or the evidence-grid extension; keyword rows mint one span per row so the 12-character minimum quote holds and the unit stays in the evidence; the Patent Agent's `Field.status` carries `needs_counsel`.

### Amendments from the build (26 Nov 2026, Proposed)

1. B1, the reader (D1). `RecordReader` (`grounding/ports.py`) reads one versioned source's records: `read(source, record_id)` returns a `ReturnedText` at the record's current version, `records(source, collection)` lists IDs. The loop gives each dispatched attempt a fresh `AttemptReader` in `ToolContext.reader` and mints the body's drafts over exactly the texts it returned; with no reader configured every read is None and a draft is refused as before. The composition root picks the adapter; `adapters/sources/folder.py` serves a folder of files, versioned by a hash of their bytes.
2. B2, the model sees what it may cite. A span is minted after the result is rendered, so the view cannot carry its ID. The step records each draft's span ID in draft order (`value["spans"]`), and the tool message shows `{"citation_spans": [...]}` after the result. An answer to an input interrupt names its statement span in the answers block, and an unknown read line names the blocker's failed steps, so a reader can save `Unknown(reason="unverified", failed_step=...)`.
3. B3, the composition root wires the reader (B1, 26 Nov 2026). `app.open_session` gives the session one `FolderSources` reader over every versioned source the loaded agents and their extensions declare: a source's folder is its `AKSHA_SOURCES` setting (`name=folder` pairs), else its spec's `root`. A source with neither has no folder and reads as none; a setting that names no declared source is refused `source_unknown`. The session passes the reader to each run's loop setup, so `aksha run` reads a world's records, for example `AKSHA_SOURCES=corpus=fixtures/worlds/stillroom/sources` for the research agent and `patent_sources=...` for the patent agent, whose source world reads through the call's reader when it is given no folder of its own.
4. B4, a resumed instance's evidence (decision 36). `grounding.facts.evidence` names the session an instance resumed (its latest `task.entered{reentry}` in the run, when the mode is `resume`), and a span shown to one of that session's instances counts as shown to it (`span_not_seen` otherwise). The resumed context shows those spans again; a span whose record moved is still refused `stale_source`, and a fresh entry gets no such allowance. Proposed.

## Alternatives considered

| Option | What it is | For | Against | Verdict |
|---|---|---|---|---|
| A. Whole-passage citations by ID | Cite a passage; no quote | Simple | Cannot show which words support the claim; a long passage supports anything | Rejected |
| B. Model-written source list, checked afterwards | The model lists sources; a checker runs after the save | Least tool work | A bad artifact exists before the check; the check needs minted spans anyway | Rejected |
| C. Offsets validated for bounds only | Each value carries offsets into the source | Cheap | In-range offsets over the wrong text pass | Rejected; offsets are kept as the position selector beside a verified quote |
| D. Provider citations trusted as returned | Use the model's own citation blocks | No minting code | Provider-specific; a fake model cannot produce them; trust moves to the provider | Rejected as the authority; adapted as hints (decision 7) |
| E. Fuzzy re-anchoring of quotes | Re-find a quote after the source text moves | Survives small source edits | Fuzzy matching weakens "verbatim"; a moved quote is better treated as invalidation | Later, for display only (cut M10) |
| F. Tool-minted spans, verbatim verification at save, explicit unknowns (my lean) | Decisions 1 to 8 | the product requirements becomes a refused save; works for any agent and any versioned source | Every read tool must mint; quotes need a minimum length; span text grows the event store | Chosen |

## Interface

```python
# aksha/runtime/grounding.py (proposal; design only). Exported through aksha/runtime/api.py.
@dataclass(frozen=True)
class SpanDraft:                                     # returned by a tool body
    source: str                                      # "fixtures", "artifact", "tree", "exec", "statement"
    record_id: str                                   # record, artifact ref, "tree@<hash>:src/app.py", step ID
    version: str
    start: int                                       # code points into the canonical text
    end: int

@dataclass(frozen=True)
class CitationSpan:                                  # minted on the runtime thread
    span_id: str                                     # "EX-0042"
    tenant: str
    source: str
    record_id: str
    version: str
    start: int
    end: int
    text: str                                        # the TextQuoteSelector's exact text
    shown_to: frozenset[str]                         # S1: instances whose shown text contained it

@dataclass(frozen=True)
class Unknown:                                       # an explicit unknown, valid only on may_be_unknown fields
    question: str
    reason: Literal["not_found", "unverified", "not_checked", "not_located"]
    checked: tuple[str, ...] = ()                    # record versions read in full, for "not_found"
    failed_step: str | None = None                   # for "unverified"

CiteForm = Union["SpanCite", "StatementCite", "ArtifactCite"]

@dataclass(frozen=True)
class CitePolicy:                                    # declared per field on an artifact kind
    field: str                                       # JSON Pointer pattern, "/cells/*/values/*"
    forms: frozenset[Literal["span", "statement", "artifact"]]
    span_sources: frozenset[str] = frozenset()       # tools or sources whose spans this field may cite
    artifact_kinds: frozenset[str] = frozenset()
    may_be_unknown: bool = False

def mint_spans(drafts: Sequence[SpanDraft], returned: "ReadSet", instance: str,
               tenant: str) -> tuple[CitationSpan, ...]: ...           # raises class_violation
def verify_citation(cite: CiteForm, spans: "SpanIndex", instance: str,
                    sources: "SourceVersions", policy: CitePolicy) -> "Verified | CiteError": ...  # pure
def verify_content(content: Mapping[str, Any], kind: "ArtifactKind", spans: "SpanIndex",
                   instance: str, sources: "SourceVersions") -> tuple["Verified | CiteError", ...]: ...  # pure

MIN_QUOTE_CHARS = 12
CiteCode = Literal["unknown_span", "span_not_seen", "stale_source", "quote_not_verbatim",
                   "quote_too_short", "form_not_allowed", "superseded_statement",
                   "stale_artifact_cite", "ungrounded_value"]
# v2's unknown_excerpt and excerpt_not_seen are renamed unknown_span and span_not_seen;
# ungrounded_value and form_not_allowed are new. Codes settle with the event-store ADR.
```

Events it writes (v2 names, until the event-store ADR): `excerpts.minted` with each span's full text, in the transaction of the step that returned it; `citation.bound` for each verified citation, in the save's transaction. A refused save writes nothing of its own; the step's `step.completed` carries the error code.

Projections it reads: The span index (spans by tenant and by instance they were shown to), the current version of each source (`VersionedSource.version`) and the current version of each cited artifact. All are rebuilt from the event store by reducers.

## Stress test (25 Nov 2026)

Three design reviews in the three stress reviews walked the Research Agent (the Research stress review), the Patent Agent (the Patent stress review) and the contrast coding agent (the coding stress review) through this ADR on 25 Nov 2026. Nothing was built or run; each verdict is a reading of the interfaces. The verdict values are: fits; fits with agent-side code (the core is unchanged); needs a runtime change (generic, with no agent word in the core); missing primitive (no ADR owned it).

| Agent | Verdict as written | The scenario | What this ADR now says |
|---|---|---|---|
| Research | Needs a runtime change | The model cites span `EX-0042` over a pricing table that truncation hid | S1 `shown_to` narrowed to shown text; `Unknown(reason="unverified")` cells and `claim_status` stay as they are |
| Patent | Needs a runtime change | One new contributor record moves the source; C2's re-save citing unchanged CON-07 is refused | S2 record-grain `stale_source`; S3 optional `value_in_quote` |
| Coding | Needs a runtime change | A citation into `src/auth.py` goes stale when `utils.py` changes | S2 with ADR-P04 S3: The span's version is the file's entry hash |

Verdict after the amendments: fits for all three once S1 and S2 land. S2 is first-milestone work (the Patent demo); S1 is small and should ride with ADR-P08 S1.

Open for the maintainer: none new.

## Sources (public)

1. The quote sheet in the design notes: Rashkin and others, "Measuring Attribution in Natural Language Generation Models", Computational Linguistics (arXiv 2021, journal 2023), abstract; Liu, Zhang and Liang, "Evaluating Verifiability in Generative Search Engines", Findings of EMNLP 2023, abstract.
1. Robert Sanderson, Paolo Ciccarese, Benjamin Young (eds.), "Web Annotation Data Model", W3C Recommendation, 23 Feb 2017.
