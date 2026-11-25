# ADR-P07: Model interface and model routing by class (three owned contracts, extraction, classification and generation, bound to adapters through model aliases, with confidence thresholds and an abstention band)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. No decisions log entry settles the model interface or model classes; every choice here is my lean and waits for the maintainer. Two recorded calls shape it: item 6 (extraction produces typed artifacts with excerpt references, which an extraction class serves) and item 11 (industry names, including "model routing by class" and renaming `decide` to classification). The glossary left the code names to this ADR; I set them below. Nothing was built as of 25 Nov 2026. Amended 25 Nov 2026 after the three-agent stress test: S1 and S2 below are new and Proposed.

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
4. Code applies every confidence threshold; a model never does. A fast-class answer is accepted only at or above `accept_at` (and, for extraction, only with a span inside the minted citation spans it was given); otherwise it abstains. Each threshold carries `fitted_on`, `None` until a labelled set exists, and the run inspector lists every unfitted threshold, so nothing implies a calibration nobody measured.
5. Fallback never crosses a class silently:

| From | Allowed fallback | Rule |
|---|---|---|
| Generation | Another generation adapter | On a transient error, overflow or refusal, capped per step |
| Native classification | Another native classification adapter | Once a native candidate is in the chain, only native candidates follow |
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
class ConfidenceThresholds:
    accept_at: float = 0.9               # placeholder, unfitted
    reject_at: float = 0.2               # boolean questions: P(true) at or under this accepts False
    null_at: float = 0.5                 # extraction: p_null at or over this means "absent"
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
    value: str | float | list | None
    span: "SpanRef | tuple[SpanRef, ...] | None"  # code points into a minted citation span
    p_null: float | None
    confidence: float | None

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
    probabilities: tuple[tuple[str, float], ...]  # full distribution, sorted

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
| `step.completed` | The result, the threshold outcome and reason for each fast-class answer, usage |
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
