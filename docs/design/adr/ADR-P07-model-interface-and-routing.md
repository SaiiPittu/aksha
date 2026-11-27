# ADR-P07: Model interface and model routing by class (three owned contracts, extraction, classification and generation, bound to adapters through model aliases, with confidence thresholds and an abstention band)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. No decisions log entry settles the model interface or model classes; every choice here is my lean and waits for the maintainer. Two recorded calls shape it: item 6 (extraction produces typed artifacts with excerpt references, which an extraction class serves) and item 11 (industry names, including "model routing by class" and renaming `decide` to classification). The glossary left the code names to this ADR; I set them below. Nothing was built as of 25 Nov 2026. Amended 25 Nov 2026 after the three-agent stress test: S1 and S2 below are new and Proposed. Amended 26 Nov 2026 by the general agents: G1 to G3 below (fast-class calls from a tool body, live mode, and decide, schema, extract with a general-model fallback) are new and Proposed. Amended again 26 Nov 2026 after the generality critique: G4 below (a record holding several kinds is decided section by section) is new and Proposed.

## Terms

Primitive: PR-07, cluster B (Thinking). Depends on PR-01 (every model call is a recorded step). Needed by PR-06 (agent loop), PR-08 (summary compaction calls a generation alias), PR-13 (verification of citations), PR-22 (fake and replay models). Older Aksha names: "model seam", "model kinds" (`extract`, `decide`, `generate`), "roles", "bands". Carries forward ADR-005 and ADR-019 of the v2 ADRs, HLD section 9, LLD 05c sections 3 and 4, and the fast-and-slow design note.

Terms used here:

- **Model interface** is one owned request type in and one owned result type out per model class; each provider's adapter translates to and from them, so no vendor type enters the runtime core or the event store.
- **Model class** is the contract a model answers. **Extraction**: fields in, typed values with source spans out, or null with a probability that the value is absent. **Classification**: questions in, one listed option or a boolean with a full probability distribution out. **Generation**: messages and tools in, text and tool calls out, parsed and checked, with no reliable probability.
- **Fast and slow**: extraction and classification are the fast classes (System 1 in Kahneman's terms), narrow and thresholdable; generation is the slow class (System 2). This is what "a harness that thinks fast and slow" means in code.
- **Model alias** is a purpose name an agent declares, such as `classify.support` or `generate`, bound to one class. The run mode binds each alias to one adapter.
- **Run mode** is `fake` (each agent's rule-based adapters, the default), `scripted` (fixed replies for unit tests), `replay` (recorded replies from an earlier run) or `live`.
- **Confidence thresholds** are per-field or per-question numbers (`accept_at`, `reject_at`, `null_at`, `margin`) that code applies to a fast-class answer. Between accept and reject lies the **abstention band**: The model's answer is not used and the field becomes an explicit unknown or goes to a person.
- **Native** and **adapted**: a native adapter returns the class's own probabilities and spans; an adapted one is a generation model under a schema, recorded `native=False`.
- **Model cascade** is the ordered chain of adapters one alias tries when an answer abstains.

## Context: what breaks without it

1. Without an owned interface, a vendor's types leak into the event store and the runtime core, and swapping a live model for a fake one needs code changes. The product requirements require a fake-model mode, a deterministic replay mode and no paid credentials.
2. Most agents have jobs that are not generation. The Patent Agent's "does this claim disclose feature F2?" is a classification with a probability; the Research Agent's "list price in this row" is an extraction with a span; the contrast coding agent's "is this test failure flaky?" is a classification. Forced through a chat call, each loses the typed answer and the number that code can threshold, so "never invent an answer" becomes a prompt instruction instead of a rule.
3. Without routing rules, fallback crosses contracts silently. If an extraction adapter fails and a generation model fills in, the artifact still carries a "span", but no model located it, and the citation check of PR-13 is then checking a guess.
4. Without a recorded binding, a resumed run can pick a different adapter than the one it started with, and its recorded steps no longer describe what answered them.

## Decision

My lean:

1. Three protocols, one per model class, each with its own operation: `GenerationModel.generate`, `ExtractionModel.extract`, `ClassificationModel.classify`. The request and result types are Aksha's own frozen dataclasses. This renames v2's `Model.next`, `Extractor` and `Decider`.
2. Routing is by declaration, not by request content. An agent declares model aliases with their class; a Task, a verification rule or a fast-class tool names the alias it uses; the generation alias of every Task is implicit and named `generate`. At load, the run mode binds each alias to one adapter, the loader refuses an adapter whose class differs from the alias's class (`model_class_mismatch`) and a field or question type the adapter does not support (`unsupported_type`), and the binding is hashed into the agent definition version hash, so a run cannot resume under a different binding.
3. Every adapter carries a capability record: native or adapted, the field and question types it supports, whether it returns spans and distributions, whether it works offline. The loader and the cascade branch on these fields, never on the adapter's name or vendor. This is the session API's capability record (PR-19) applied to models: each candidate is classified by its model API, never by who provides it.
4. Code applies every confidence threshold; a model never does. At the event boundary every fast-class number is a fixed-point integer, because `step.completed` carries the answers and the event store refuses floats in hashed payloads (ADR-P01 H3): a probability, `p_null` or confidence is an integer in parts per million (0 to 1,000,000), and a `number` field's value is a decimal string. Adapters convert at their edge, and threshold checks and replay compare the integers, so two runs give identical bytes. Thresholds are written as fractions in the agent definition (whose hash uses `canonical_json`, which allows them) and are converted to parts per million once at load (`ppm(0.9) = 900000`). A fast-class answer is accepted only at or above `accept_at` (and, for extraction, only with a span inside the minted citation spans it was given); otherwise it abstains. Each threshold carries `fitted_on`, `None` until a labelled set exists, and the run inspector lists every unfitted threshold, so nothing implies a calibration nobody measured.
5. Fallback never crosses a class silently:

| From | Allowed fallback | Rule |
|---|---|---|
| Generation | Another generation adapter | On a transient error, overflow or refusal, capped per step |
| Native classification | Another native classification adapter | Natives come first in the chain; after them only an adapted classifier may follow, and only when the alias sets `allow_adapted` and the question gates no external effect (next row) |
| Native classification | An adapted classifier (a generation model under a forced-choice schema) | Only when the alias sets `allow_adapted`; recorded `native=False`; judged by its own thresholds; never for a question that gates an external effect |
| Extraction | Another native extraction adapter | An adapted extractor returns no spans, so it never stands in for a cited field |
| Any fast-class alias | A generation call in its place | Not allowed |
| Any model answer | Gating an external effect | Not allowed; only a person's approval does (PR-17) |

6. When a cascade is used up, the default is an explicit unknown with a question for a machine-only artifact, and a review request for a human-reviewed one (pattern P-4 extended to abstention; FS-5 lean B).
7. Every model call, of every class, is a durable step with a deterministic step ID (PR-02), so a resume returns the recorded answer and a second fake-mode run writes the same bytes. Fast-class calls are never made inside a completion check body, so completion checks stay pure (P-3). A fast-class answer can only refuse what a code rule allowed, never allow what a code rule refused.
8. Transient provider errors are retried twice with backoff on the injected clock; after that the step fails `model_unavailable`. Usage is recorded on the model step by the runtime and only there, so no agent writes its own cost.
9. A live adapter is imported only in `live` mode, named by an environment variable as `module:Class`; its key is read in `__init__`, held on the instance, and never written to an event, an error message or a `repr`. `run.started` records the alias, the adapter class and `native`, never configuration.

Worked example, the Patent Agent's support question on cell F2 against family A, with placeholder thresholds (unfitted): native `accept_at` 0.9, adapted `accept_at` 0.85.

| Step | Adapter | Answer | Thresholds used | Outcome |
|---|---|---|---|---|
| `.../c02/d01` | Native, scripted | `partial` at 0.40 | Native, 0.9 | Abstain: 0.40 is under 0.9 |
| `.../c02/d02` | Adapted, scripted, `native=False` | `partial` at 0.90 | Adapted, 0.85 | Accept `partial` |
| Compare, in code | none | Claim `partial`, accepted `partial` | none | Located; the save goes ahead |
| Variant of d02 | Adapted | `partial` at 0.60 | Adapted, 0.85 | Abstain; cascade used up; the cell becomes an explicit unknown with a question |

The question gates no effect, which is why the adapted step was allowed; the same question on a send target would stop at d01.

### Amendments from the stress test (25 Nov 2026, Proposed)

1. S1, classification options may come from a saved artifact. "What do customers complain about?" has no option list in advance. The workable shape is two steps: a generation step saves a theme list with a citation per theme, then classification labels each review against it. Static options in the agent definition cannot express the second step, and v2's `DecideTool.questions` pure function (05c line 456) was not carried forward. `QuestionSpec` gains an option source, read like ADR-P15's key sources, so the options are recorded data with lineage:

   ```python
   # aksha/runtime/models.py (proposal)
   @dataclass(frozen=True)
   class QuestionSpec:
       prompt: str
       options: tuple[str, ...] = ()                    # static options, or
       options_from: "KeySource | None" = None          # options read from a saved artifact (S1)
       thresholds: "ConfidenceThresholds" = ...         # accept_at and the abstention band
       margin: float | None = None
   # Loader: exactly one of options and options_from; the options' artifact ref joins state_inputs.
   ```

2. S2, the request router is a runtime classification alias. Choosing an agent for a free-text request is a classification over the loaded agents' `purpose` text with the abstention band of this ADR as its trigger to ask. It is designed in ADR-P23; the alias `runtime.route` is the one classification alias the runtime core itself owns.

Optional, not adopted: a core `quantity` field type (amount, unit span). The Research Agent extracts `amount`, `currency` and `billing_period` as three fields, each with its own span, which works.

### Amendments from the general agents (26 Nov 2026, Proposed)

These record what the research and patent agents needed to read a person's own uploads, whatever their structure. All three are Proposed and wait for the maintainer.

1. G1, fast-class calls from a tool body. A read tool that must understand what a record holds asks a fast-class alias instead of matching a layout in code. `ToolSpec.model_aliases` names the agent's extraction and classification aliases a body may ask; the loader refuses one the agent does not declare with that class (`model_alias_unknown`). The loop gives the body `ToolContext.models` (`runtime/models/calls.py`, `ToolModelCalls`), which runs the alias's cascade under D4's thresholds and D5's rules and records every call on the tool step's `step.completed` under `model_calls` (per alias: calls, adapters, accepted, abstained, exhausted, failures, the summed `usage`, and one hash over every request and answer). The run's token and money budgets count that `usage` like a model step's (ADR-P14 D9, `budgets/records.usage`), so a body that asks a fast model once per row spends the same budget as the loop's own calls. The tool step is the durable unit (D7): a completed step returns its recorded result, so a resume asks nothing again and a fake-mode run writes the same bytes; a transient model error is the step's retry. A recorded query re-executed on a source change asks through the same binding (`app.ToolQueryRunner`).
2. G2, live mode wired (D9). `AKSHA_MODE=live` binds every alias of every loaded agent. `AKSHA_ALIAS_<NAME>=module:Class` names an alias's adapter (a native System 1 service, or any class built as `cls(alias, env)`); an alias with none takes the built-in adapter of its class in `adapters/models/live.py`, over `AKSHA_LIVE_BASE_URL`, any server that speaks the chat-completions shape. The model is configuration per alias: `AKSHA_LIVE_MODEL_<ALIAS>`, else `AKSHA_LIVE_MODEL_<CLASS>`, else `AKSHA_LIVE_MODEL`. The start is refused before any call with `live_config_missing` (no server or no model for an alias) or `live_key_missing` (no `AKSHA_LIVE_API_KEY` for a server that is not on this machine), each refusal naming the setting to set. The key is read from the environment only, sent only in the `Authorization` header and never recorded; `run.started` records each alias's adapter class and `native`.
3. G3, reading a record nobody described in advance: decide, pick the schema, extract (`runtime/models/documents.py`). A person uploads whatever they have, so one fixed extraction schema cannot fit. An agent declares its `DocumentKind`s, each with a description and the extraction alias whose declared fields are that kind's typed schema, and one classification alias to decide between them. Per record: (a) **decide**: one choice question over the declared kinds and `other`, its state a preview code renders from structure alone (`document_preview`: name, format, headings, header fields, a table's columns and sample values, the start of the text), judged by the question's thresholds, so an answer in the abstention band reads `other`; (b) **pick the schema** of the decided kind; (c) **extract** it, each value with a span inside the passages it was shown, which the grounding layer mints and verifies verbatim (ADR-P13 D2). (d) What the fast steps leave goes to the System 2 model: a record of kind `other` or undecided, and every schema field the extractor abstained on, are listed as `unresolved`, and the tool returns the record's passages, one citation span each, so the Task's general model reads and cites them itself. Nothing a fast model abstained on is guessed in code. The field types follow the typed-extraction contract: `text`, `number`, `date`, `time`, `datetime`, `duration`, `list`, `estimate` and `forecast` (`FieldSpec.type`; `type_ok` checks each form).
4. G4, a record that holds several kinds (`record_sections`, `section_heading`). People upload mixed documents: one memo with a section on who sells what, a section of customer quotes and a section of plans; a saved page with a price table above its reviews. G3 answers one kind per record, so such a record lost every kind but one (offline it found no company at all). A read tool now cuts a text record into sections by headings alone (every heading of the shallowest level that occurs at least twice, else every heading; a section with no content joins the next; at most 12) and decides each section on its own with the same question and thresholds. A listing shows each section's kind beside the record's; a read tool takes a section number and decides, extracts and cites within that section only, its ranges still the record's. A table, and a text with fewer than two sections, is one section. A whole record whose sections disagree is usually decided inside the abstention band, so it reads `other` and its sections carry the kinds. Open: a section is a reading unit, not a versioned record part (ADR-P05 D6), so an edit to one section reopens all work on the record.

   The general-model fallback is equally first-class. With no native System 1 model configured, a general model fills the same decide and extract contracts in **adapted mode**: the built-in live extraction and classification adapters ask it for a forced JSON schema and are recorded `native=False`. D5's table changes in one row: an adapted extractor may now answer a cited field, because its spans are not its own claim; code locates each verbatim quote in the passage it names (whitespace folded, nothing else), a quote it made up is located nowhere and the field abstains `span_missing` (the `adapted_no_span` reason is gone). `allow_adapted` now covers extraction as well as classification, an adapted answer is judged by the field's or question's `adapted` thresholds (`FieldSpec.adapted` is new; listed by `unfitted_thresholds`), and an adapted answer still never gates an external effect. So a person with only one general model runs the same pipeline, with the same citation verification, as one with a native extractor; only the calibration differs, and every threshold says it is unfitted.

   | From | Allowed fallback (amended row) | Rule |
   |---|---|---|
   | Extraction | An adapted extractor (a general model under a forced schema) | Only when the alias sets `allow_adapted`; its spans are where code located its verbatim quotes; judged by the adapted thresholds; never for a field that gates an external effect |

   Offline, each agent's `fakes_readers.py` implements the same contracts with general rules over the request alone (the decide rules over the preview, each kind's schema over its passages), never a lexicon of one world; the third fixture world, `cindral`, has inputs of other shapes than the first two and passes with the same code. Proposed.

5. G5, a record of several things, read unit by unit (27 Nov 2026, Proposed). The held-out worlds hold several invention candidates in one record (a notebook's pages, an XML tracker's entries, a JSON tracker's issues) and several publications in one (a search export's hits, a search report's sections), so G3's one reading per record found at most one, and one shared record could not be grouped into families. The patent agent now cuts a record into units by structure (`world.record_units`: each object of a JSON record's largest list of objects, each repeated XML element, the parts between two or more rule lines, else G4's sections), decides the record's kind once, and extracts the kind's schema unit by unit over the passages inside it. Units whose head names the same identifier, or that name none, are one group (a notebook's later page on an idea joins its first); with one group the record is read whole, so a record of one thing reads as before. A unit group with mechanism steps is a candidate; its ID is the one the extraction read, unless that ID is named by no other input and another identifier in the same text is (a ticket key beside the venture's own ID). Each unit of a publication record that states a number is one publication, and family grouping reads the priority claims wherever they sit (a timeline event, a list of priority objects, a heading's number and kind code). Steps keep the labels the document gives them (`E2`), else are numbered `S1`... in the order of their list marks (`(b2)` after `(b)`), and a step whose own line carries another date than the record's keeps that date. A disclosure register written as blocks of `Key: value` lines, as JSON, JSON Lines or YAML is a table like any other, and a write-up no row links is joined to the one row whose date it states. The note schema gains `opinions` (a person's view of how strong the idea is, reported, never a finding). The offline fakes implement the same contracts with general rules over structure (headings, rule lines and pages, list marks and labels, speaker turns, record fields, units of measure), never per world. Open: what a matrix cell holds is the comparison model's judgement; offline it counts a step's distinctive words in a family's text, which reads a paraphrase as partial (`docs/limitations.md` item 19).

## Alternatives considered

| Option | For | Against |
|---|---|---|
| A. Generation only, with per-purpose model settings | One interface; every provider supports it | No typed answer and no probability; thresholds become prompt text |
| B. A router over model groups with fallbacks by error class | Mature routing, many providers | Routes generation calls only; a dependency in the core; vendor shapes reach the event store |
| C. Every typed answer as a forced tool call or structured output with re-ask | No new interface | Loses distributions and spans; a fuzzy match is not a located span |
| D. One fast interface for both extraction and classification | Fewer types | The loader cannot refuse a class mismatch between the two |
| E. One contract per class, aliases bound by mode, capability-checked, native-only fallback (my lean) | A mismatch is a load error; fakes per class; fallback rules visible in the event store | Three protocols instead of one; thresholds are placeholders until measured |

A second axis, routing by request content (a local scorer rates each request and picks a model), I leave out of v1: in Aksha the Task declares its alias, so the routing decision is reviewable data. An agent that wants content routing can declare a classification alias whose answer selects a Task, which keeps the decision a recorded step.

Takeaway: non-generation models belong behind their own operation, with silent substitution refused; Aksha's addition is making the binding, the cascade and every abstention facts in the event store.

## Interface

### Types and protocols

```python
# aksha/runtime/models.py (proposal; design only)
ModelClass = Literal["extraction", "classification", "generation"]
RunMode = Literal["fake", "scripted", "replay", "live"]
FieldType = Literal["text", "number", "date", "duration", "list"]
QuestionType = Literal["choice", "boolean", "score"]
OnExhausted = Literal["unknown", "review", "fail"]
FinishReason = Literal["tool_calls", "text", "length", "refusal"]

@dataclass(frozen=True)
class ModelCapabilities:                 # the capability record of one adapter
    native: bool                         # False: a generation model under a schema
    field_types: frozenset[FieldType] = frozenset()
    question_types: frozenset[QuestionType] = frozenset()
    returns_spans: bool = False
    returns_distribution: bool = False
    offline: bool = True                 # True for fake, scripted and replay adapters

@dataclass(frozen=True)
class ConfidenceThresholds:              # fractions in the definition; compared as ppm after load (D4)
    accept_at: float = 0.9               # placeholder, unfitted
    reject_at: float = 0.2               # boolean questions: P(true) at or under this accepts False
    null_at: float = 0.5                 # extraction: p_null_ppm at or over ppm(null_at) means "absent"
    margin: float | None = None          # choice: minimum gap between the top two options
    fitted_on: str | None = None         # hash of the labelled set; None means unfitted

@dataclass(frozen=True)
class ModelAlias:
    name: str                            # "classify.support"; namespaced by the agent at load
    model_class: ModelClass
    fallback: tuple[str, ...] = ()       # other aliases of the same class, in order
    allow_adapted: bool = False          # classification only
    on_exhausted: OnExhausted | None = None   # None: "review" for reviewable kinds, else "unknown"

# Generation (the slow class)
@dataclass(frozen=True)
class GenerationRequest:
    step_id: str                         # "R-0002/compare[C1]/t03"
    messages: tuple["Message", ...]
    tools: tuple["ToolSpecView", ...]
    tool_choice: str | None = None       # a hint the forced-tool stop policy may set; fakes ignore it
    context_hash: str = ""               # sha256 of canonical messages, tools, tool_choice (PR-08)
    context_chars: int = 0

@dataclass(frozen=True)
class GenerationResult:
    text: str | None
    tool_calls: tuple["ToolCall", ...]
    finish: FinishReason
    usage: "Usage"

class GenerationModel(Protocol):
    model_class: ClassVar[Literal["generation"]]
    capabilities: ClassVar[ModelCapabilities]
    def generate(self, request: GenerationRequest) -> GenerationResult: ...
        # raises TransientModelError (retried), PermanentModelError, ContextOverflow

# Extraction and classification (the fast classes)
@dataclass(frozen=True)
class ExtractionRequest:
    step_id: str
    alias: str
    state: tuple["CitationSpan", ...]    # minted citation spans, canonical text (PR-13)
    fields: tuple[tuple[str, "FieldSpec"], ...]   # FieldSpec carries its ConfidenceThresholds
    request_hash: str

@dataclass(frozen=True)
class FieldAnswer:
    value: str | int | list | None      # a number field is a decimal string ("12.50"), never a float (D4)
    span: "SpanRef | tuple[SpanRef, ...] | None"  # code points into a minted citation span
    p_null_ppm: int | None               # parts per million, 0..1_000_000 (D4)
    confidence_ppm: int | None

@dataclass(frozen=True)
class ExtractionResult:
    answers: tuple[tuple[str, FieldAnswer], ...]
    adapter: str
    native: bool

@dataclass(frozen=True)
class ClassificationRequest:
    step_id: str
    alias: str
    state: str                           # rendered by code from citation spans or artifacts, never by a model
    state_inputs: tuple[str, ...]        # citation span IDs and artifact refs behind the state (lineage)
    questions: tuple[tuple[str, "QuestionSpec"], ...]
    request_hash: str

@dataclass(frozen=True)
class ClassAnswer:
    choice: str | bool | None            # None: The adapter refused
    probabilities_ppm: tuple[tuple[str, int], ...]  # full distribution in parts per million, sorted (D4)

@dataclass(frozen=True)
class ClassificationResult:
    answers: tuple[tuple[str, ClassAnswer], ...]
    adapter: str
    native: bool

class ExtractionModel(Protocol):
    model_class: ClassVar[Literal["extraction"]]
    capabilities: ClassVar[ModelCapabilities]
    def extract(self, request: ExtractionRequest) -> ExtractionResult: ...

class ClassificationModel(Protocol):
    model_class: ClassVar[Literal["classification"]]
    capabilities: ClassVar[ModelCapabilities]
    def classify(self, request: ClassificationRequest) -> ClassificationResult: ...

Adapter = GenerationModel | ExtractionModel | ClassificationModel
```

### Binding, thresholds and cascades

```python
@dataclass(frozen=True)
class AliasTable:
    mode: RunMode
    adapters: tuple[tuple[str, Adapter], ...]          # alias -> adapter instance
    record: tuple[tuple[str, str, bool], ...]          # (alias, adapter class, native) for run.started

def bind_aliases(defn: "AgentDefinition", mode: RunMode, env: Mapping[str, str],
                 registry: "AdapterRegistry") -> AliasTable: ...
    # refuses unknown_alias, model_class_mismatch, unsupported_type; imports live classes in live mode only

ThresholdOutcome = tuple[Literal["accept", "abstain"], object | None, str]   # (outcome, value, reason)
def threshold_extraction(answer: FieldAnswer, spec: "FieldSpec", native: bool) -> ThresholdOutcome: ...   # pure
def threshold_classification(answer: ClassAnswer, spec: "QuestionSpec", native: bool) -> ThresholdOutcome: ...  # pure

@dataclass(frozen=True)
class Candidate:
    alias: str
    adapter: Adapter
    native: bool

def cascade_for(alias: ModelAlias, table: AliasTable, gates_effect: bool) -> tuple[Candidate, ...]: ...  # pure
def run_cascade(executor: "DurableStepExecutor", base_step_id: str, chain: tuple[Candidate, ...],
                request: ExtractionRequest | ClassificationRequest) -> "CascadeResult": ...

def model_step(view: "RunView", instance: "InstanceRef", prep: "Prep", table: AliasTable,
               executor: "DurableStepExecutor", policy: "ContextPolicy") -> "ModelStepResult": ...
```

Adapters per class and mode:

| Class | `fake` (default) | `scripted` | `replay` (later) | `live` | Adapted |
|---|---|---|---|---|---|
| Generation | A rule-based policy per agent, in the agent's `fakes.py`, reading only tool results in its context | `ScriptedGenerationModel` | `ReplayGenerationModel` | `LiveGenerationModel` over a chat-completions-compatible HTTP API; `LocalGenerationModel` for a localhost server | Not applicable |
| Extraction | `RuleExtractionModel` subclass | `ScriptedExtractionModel` | `ReplayExtractionModel` | Named per alias by `AKSHA_ALIAS_<NAME>` | None |
| Classification | `RuleClassificationModel` subclass, with declared constant distributions | `ScriptedClassificationModel` | `ReplayClassificationModel` | Named per alias | `AdaptedClassificationModel(generation_model)`, `native=False` |

### Events it writes

No new event types. Through the durable step executor:

| Event (v2 catalogue name) | Carries for a model step |
|---|---|
| `run.started` | The alias table record: alias, adapter class, native; never configuration or keys |
| `step.started` | Step ID, `model_class`, alias, adapter, native, `replaces` (the previous candidate's step ID in a cascade), `args_hash` (`context_hash` or `request_hash`) |
| `step.completed` | The result, the threshold outcome and reason for each fast-class answer, usage; every probability and confidence as an integer in parts per million, every number value as a decimal string (D4) |
| `step.failed` | `model_transient` attempts; `model_unavailable` when a cascade candidate fails permanently |

A cascade needs no event type of its own: each candidate is a step whose `replaces` field names the one before it (P-2).

### Projections it reads

| Projection | Owner | Use |
|---|---|---|
| Recorded steps by step ID | PR-01, PR-02 | Return a recorded answer on resume; detect `step_mismatch` |
| Alias table on `run.started` | This primitive | Refuse a resume under a different binding (with the definition version hash) |
| Artifact store, minted citation spans | PR-04, PR-13 | The state of an extraction request; span bounds checks |
| Budget accounting | PR-14 | Active time of fast-class steps counts; they do not count as tool calls |

## Stress test (25 Nov 2026)

Three design reviews in the three stress reviews walked the Research Agent (the Research stress review), the Patent Agent (the Patent stress review) and the contrast coding agent (the coding stress review) through this ADR on 25 Nov 2026. Nothing was built or run; each verdict is a reading of the interfaces. The verdict values are: fits; fits with agent-side code (the core is unchanged); needs a runtime change (generic, with no agent word in the core); missing primitive (no ADR owned it).

| Agent | Verdict as written | The scenario | What this ADR now says |
|---|---|---|---|
| Research | Needs a runtime change | Complaint themes cannot be declared as fixed options; prices need units | S1 `options_from`; units stay agent-side as three fields |
| Patent | Fits with agent-side code | the first milestone's loader refuses the three fast-class aliases with `model_class_not_built` | the first milestone runs those judgements as rule code inside the agent's tools; no `score` field exists in any schema |
| Coding | Fits | "Is this failure flaky?" under `classify.flaky` | Nothing new; the completion check still reads the test report |

Verdict after the amendments: fits for all three once S1 lands. S1 is later (extraction and classification are cut row 8); S2 is first-milestone work through ADR-P23.

Open for the maintainer: whether a core `quantity` type is worth its cost (lean: no for v1).

## Sources (public)

1. Daniel Kahneman, *Thinking, Fast and Slow*, 2011, for the System 1 and System 2 terms, by way of the fast-and-slow models note.
