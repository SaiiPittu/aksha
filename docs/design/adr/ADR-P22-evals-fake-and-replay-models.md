# ADR-P22: Evals, fake models and replay models (a fake for every model class, replay read from the recorded event store, eval cases as data with a kind and a source, and pass^k only where rollouts differ)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. Nothing here was built as of 25 Nov 2026. No entry in the decisions log settles fakes, replay or evals; D13-3 (a case kind and a source on every eval case) is a decision for the maintainer that is not applied. The maintainer's calls this ADR applies without reopening: Aksha is a general harness (decision 1), agents are defined in Python with checks as pure functions (decision 2, pending his confirm), and every interpretation is a projection over the event store (P-2). Amended 25 Nov 2026 after the three-agent stress test: S1 to S3 below are new and Proposed.

## Terms

Primitive PR-22 in the primitives catalogue, cluster E (interfaces and proof). Depends on PR-02 (replay returns recorded results), PR-07 (the model interface and its three model classes) and PR-21 (graders read the same projections the trace does). Needed by no primitive in the core, and used by every ADR's tests. It refines v2 ADR-005 (the model interface and fakes), ADR-008 (test fixtures and answer keys) and ADR-010 (eval statistics); their decisions stand except where this ADR says otherwise.

Terms used here, in the industry names of the design glossary:

- **Fake model** is a model adapter that computes its reply from the request with code, so a run needs no network and no paid key. A **rule-based fake** is an agent's own rules over the tool results in its context. A **scripted model** returns a fixed list of replies per Task instance.
- **Replay model** returns the replies a recorded run received, in order, and fails at the first request that differs from the recorded one.
- **Fake model mode** binds every model alias to a fake; **replay mode** binds them to replay adapters over one recorded run; **live mode** binds them to real adapters.
- **Fault switch** is a seeded rule, kept outside every fixture, that makes a tool, model or receiver fail in a named way at a named step.
- **Eval** runs an agent over a set of **eval cases** (v2 calls them scenarios) and grades the results. An **episode** is one run of one case; a **rollout** is one of k episodes of the same case.
- **Case kind** says why a case exists: a **bad** case reproduces a failure the agent once made, a **guard** case protects a behaviour that works, and a **control** case is a baseline that should pass whatever changes. **Source** names where the case came from: a bug, a review finding, or a guidance line. These two fields are D13-3.
- **pass@k** is the chance that at least one of k rollouts passes; **pass^k** is the chance that all k pass, which measures reliability rather than luck. **k_effective** is the number of distinct traces among the k rollouts.
- **Grader** is a function that turns an episode's event store and the case's answer key into criterion results. **Ungradeable** is a verdict of its own: a grader could not evaluate a criterion, which is never counted as a pass or a fail.
- **Test fixtures** are the synthetic inputs every tool reads in fake model mode, behind the versioned data source interface; **answer keys** are the expected values, kept outside every tool's root.

## Context: without fakes and evals, a harness cannot prove its own claims

The product requirements require a deterministic fixture or replay mode and a fake-model mode, a project that runs with no paid credentials or network, fixtures that can be replaced so the workflow works on changed inputs rather than a scripted transcript, automated tests for both agents and for adding a plugin, and documentation of how a real model connects. A general harness needs the same for agents nobody has written yet.

| What breaks | How |
|---|---|
| A test passes on a script and fails on changed inputs | A scripted transcript replays fixed calls; change one fixture and the script asks for a page that no longer matters |
| Only generation can be faked | Extraction and classification calls (PR-07) have their own contracts; a chat fake cannot return a span or a probability |
| A script silently runs out or is under-used | A test passes because the model stopped early, or because half the script was never consumed |
| A regression after a prompt or fixture edit goes unnoticed | Nothing compares the new run with a known-good one, step by step |
| A deterministic fake is reported as reliability | Every rollout is identical, so pass^k equals pass^1 by construction |
| A grader bug becomes a model failure | A missing key field or a grader exception is scored 0.0 |
| The suite drifts into happy paths | Cases are added after fixes, or never, and nobody can say which real failure a case reproduces |
| Weak tests get gamed | A model exploits a grader that checks the transcript, not the state |

The contrast coding agent adds two things the product requirements agents do not: its best grader runs code (the test suite passes in the sandbox after the patch), and its sessions are long enough that a replay mismatch at turn 214 must name the step, not just say "different".

## Decision

1. **Every model class gets the same four adapters: rule-based fake, scripted, replay and live.** The model interface and its three protocols (generation, extraction, classification) belong to PR-07; this ADR fixes their test adapters. The rule-based fake is supplied by the agent (`agents/<name>/fakes.py`); the scripted, replay and recording adapters are generic and live in `aksha/fakes/`, outside the runtime core. Fakes enter a run only through the composition root, so the runtime core never knows a fake is active (05e section 2.5).
2. **Rule-based fakes obey four honesty rules** (carried from v2 ADR-005 and 05e section 9.2). A fake reads only what the request carries (tool results, rendered reads, answers, stop-policy reasons), never a file, the clock, the environment or the answer key. It never computes a value a grader checks. It reads the stop policy's continue reasons, so a vetoed finish changes its next call. It records the rule that produced each reply. A fake extractor finds values with its own rules over canonical text, never from the answer key's anchors (fast-and-slow section 2.3).
3. **The scripted model is strict in both directions.** It is keyed by Task instance; running out raises `ScriptExhausted(instance, turn)` and the run ends `failed` naming the step; `assert_complete()` fails a test that left replies unconsumed.
4. **Replay reads the recorded run's event store; there is no separate cassette.** Durable execution (PR-02) already stores every model reply on the model step's `step.completed`, keyed by the deterministic step ID with the request's `context_hash`. `ReplayModel(recorded_store, run_id)` returns those replies for a fresh run of the same case and raises `ReplayMismatch(step_id, expected_hash, actual_hash)` at the first request whose hash differs, or at the first step ID the recording lacks. Effects never run live in replay mode: receivers are the fixture receivers. A live run therefore becomes a regression test by keeping its store, which costs one paid run and no recording format.
5. **Eval cases are data with a kind and a source (D13-3 option A).** `EvalCase` replaces v2's `Scenario` and gains `kind` and `source`. The process rule: a `bad` case is committed before its fix and must fail on the code it reproduces. In the first milestone nothing is built yet, so no real failure exists to reproduce: every case is a `guard` (its source is the requirement it protects) or a `control`. The first `bad` case arrives with the first real bug, which keeps the suite honest about where its cases came from.
6. **Graders are code over the event store and the answer key, in a fixed order, and grade state, never transcripts.** The order of 05e section 12.1 stands: a gate (the store rebuilds, the key loads), then invariants, then state against the key, then citations, then the trace, then replies, and a model judge last, never gating by default. Generic invariants (one stop decision per turn end, recomputed stop decisions equal recorded ones, no Task instance entered except by the scheduler, no proposed version current without an accept, and D13-4's `inv.no_auto_start_while_held`, which PR-16 owns) live in the runtime's eval package; domain criteria live in the agent's folder.
7. **Graders prove they can fail.** A null policy (a fake that never acts) must fail every case that needs an action, and three mutants of known-good runs (`double-send`, `forged-excerpt`, `stop-decision-flipped`) must each be caught (v2 IE-8).
8. **The eval runner reports counts first, pass^k only where rollouts differ, and labels fake-mode rates.** It runs k rollouts per case, computes pass@k with Chen and others' unbiased estimator and pass^k (the chance that all k rollouts pass), and reports pass^k only where `k_effective > 1`; otherwise it prints "= pass^1 (rollouts identical)". In fake model mode, rollouts differ only under fault switches with a probability below 1, so the report says "fake model mode: rates are plumbing checks, not reliability". It also reports cost per passing episode in steps (tokens and money only when every adapter reported usage), failure clusters, a control arm with opted-in stop policies switched off, clustered standard errors by test-fixture world (none with one world), and stamps every result row with the agent definition version hash, the runtime commit and a dirty flag.
9. **Fault switches stay outside the fixtures and are keyed by step ID and attempt**, so a restarted run fails the same call again, and a fault can never fake an external effect (05e section 9.4, unchanged).

Worked example, why a deterministic fake gives no reliability number. A case is run k = 4 times with the rule-based fake and no fault switches. All four traces hash the same, so `k_effective` = 1 and the report prints pass^4 as "= pass^1 (rollouts identical)". With a fault switch that times out a fetch with probability 0.5, the four rollouts differ; say 3 of 4 pass. Then pass^2 = C(3,2) / C(4,2) = 3 / 6 = 0.50 and pass@2 = 1 - C(1,2) / C(4,2) = 1.00 (toy numbers, 05e section 11.3). That pass^2 measures the harness's recovery under seeded faults, and the report labels it so; only a live model makes it a statement about the agent.

Worked example, a regression caught by replay. A live Research run is kept as `R-0007`. A prompt edit changes the prompt of the `discover` Task. Replaying the case against `R-0007`'s store, the first model request whose `context_hash` differs is at step `R-0001/discover/t01`, so `ReplayMismatch` names that step and the run stops before any later step, telling the author exactly where the new prompt changed what the model was shown.

### Amendments from the stress test (25 Nov 2026, Proposed)

1. S1, replay returns recorded tool results too. D4 replayed model replies and ran every tool again. Fixture tools return the same bytes every time, but `pytest` output carries timings, so replaying a kept coding session raises `ReplayMismatch` at the first test run rather than at the prompt change being tested. In replay mode, read and exec tools get a replay adapter that returns the recorded `step.completed` result under the same step ID; store tools re-run against the replayed store; external tools stay on fixture receivers. This aligns D4 with ADR-P02 D11, whose replay check already assumes recorded tool results.

   ```python
   # aksha/fakes/replay.py (proposal)
   class ReplayTool:
       def __init__(self, recorded: "LogView", run_id: str) -> None: ...
       def __call__(self, ctx: "ToolContext", args: Mapping[str, Any]) -> "ToolOutcome": ...
           # returns the recorded result for ctx.step_id; raises ReplayMismatch on an args hash change
   ```

2. S2, the stress findings become cases before their fixes (D13-3). Each is a `guard` or `bad` case whose source is the stress report row, written so it fails against the ADRs as they stood on 25 Nov 2026:

   | Case | Source | Asserts |
   |---|---|---|
   | `s6_open_card` | the Patent stress review change 1 | With A-0001 `requested` when the change set lands, C1's chain rebuilds before any approval decision and `approve` is refused `target_changing` |
   | `s6_two_sources` | the Patent stress review change 2 | One change set over two sources raises one selection review, not two |
   | `research_one_vendor_blocked` | the Research stress review row 22 | `fetch_extract[COMP-04]` hits its turn limit; the run ends `partial` with 4 grounded rows and 1 explicit unknown |
   | `research_budget_finalize` | the Research stress review row 21 | `Finalize` after budget exhaustion produces a matrix with `not_checked` rows |
   | `research_duplicate_after_save` | the Research stress review row 23 | A merged duplicate's saved version is not counted in `keys="all"` |
   | `route_abstains_and_asks` | the Research stress review row 27 | An ambiguous request raises a `route` interrupt; a clear one is labelled with its agent |

3. S3, reward hacking stays a grading question. The coding agent's "tests pass" check can be gamed by editing the code under test to special-case test inputs, which ImpossibleBench reports for frontier models (Zhong, Raghunathan and Carlini, arXiv 2510.20270, abstract). Running the base tree's tests (ADR-P11 agent-side note) closes the test-editing route only. A grader for the remaining route is not designed.

## Alternatives considered

| Option | What it is | For | Against | Verdict |
|---|---|---|---|---|
| A. Scripted transcripts only | Fixed replies per test | Simplest; exact | Breaks on changed inputs | Kept for unit tests and the conformance agent |
| B. A fake that computes its reply from the messages (my lean for generation) | Rules over tool results | Input-driven, so a changed fixture changes the calls | A rule can share a bug with a domain tool; graders and answer keys guard that | Chosen, per agent |
| C. Recorded HTTP cassettes | Record provider traffic, play it back | Real model text | Vendor shapes in the recording; brittle on any fixture edit; needs a paid run | Rejected |
| D. Replay from the run's own event store (my lean for replay) | Recorded replies keyed by step ID, checked by request hash | No new format; every live run is a potential regression test; the mismatch names a step | Needs deterministic step IDs and request hashes (PR-02 provides both) | Chosen |
| E. Mock models per model class | One mock per contract | Each class tested on its own contract | Three sets of adapters | Adopted for the three classes |
| F. Eval cases as unlabelled scenarios | v2 05e section 10.1 | Already designed | A suite of happy paths with no record of why each case exists | Replaced by G |
| G. Cases with a kind and a source, added before the fix (D13-3 A, my lean) | `bad`, `guard`, `control` | Two fields; the suite becomes a record of real failures | The "before the fix" rule is a process rule a person keeps | Chosen |
| H. Unguarded pass^k | Report pass^k for every case | Familiar | Overclaims under a deterministic fake | Rejected |
| I. Guarded pass^k, ungradeable as its own verdict, clustered errors, a control arm | Decision 8 | Honest numbers | More report to read | Chosen for the designed runner |
| J. A model judge gates a pass | An LLM decides pass or fail | Covers prose quality | Puts a model inside a grader; a judge's rating says little about task success (GAUGE, provenance item 40) | Rejected as a gate; a judge may annotate |

## Interface

```python
# aksha/runtime/models.py: The three protocols belong to PR-07; shown here as the fakes see them.
class GenerationModel(Protocol):
    name: str
    def generate(self, req: "ModelRequest") -> "ModelReply": ...
class ExtractionModel(Protocol):
    name: str
    def extract(self, req: "ExtractionRequest") -> "ExtractionReply": ...
class ClassificationModel(Protocol):
    name: str
    def classify(self, req: "ClassificationRequest") -> "ClassificationReply": ...
# Every request carries step_id and context_hash; every reply carries rule: str | None (the fake's rule ID).

# aksha/fakes/ (proposal; outside the runtime core)
class ScriptExhausted(Exception):
    instance: str; turn: int

class ScriptedModel:                                   # also ScriptedExtractor, ScriptedClassifier
    def __init__(self, replies: Mapping[str, Sequence["ModelReply"]]) -> None: ...  # keyed "compare[C1]"
    def generate(self, req: "ModelRequest") -> "ModelReply": ...                    # raises ScriptExhausted
    def assert_complete(self) -> None: ...                                          # unconsumed replies fail

class ReplayMismatch(Exception):
    step_id: str; expected_hash: str | None; actual_hash: str                       # None: step not recorded

class ReplayModel:                                     # also ReplayExtractor, ReplayClassifier
    def __init__(self, recorded: "ReadOnlyStore", run_id: str) -> None: ...
    def generate(self, req: "ModelRequest") -> "ModelReply": ...                    # raises ReplayMismatch

class RulePolicy:                                      # base for an agent's rule-based generation fake
    name: str
    rules: tuple["Rule", ...]                          # first match wins; the last rule always finishes
    def generate(self, req: "ModelRequest") -> "ModelReply": ...

NULL_POLICY: RulePolicy                                # finishes at once; for grader self-tests

def wrap_model(inner: "GenerationModel", profile: "FaultProfile", seed: int) -> "GenerationModel": ...
def wrap_tool(spec: "ToolSpec", body: "ToolBody", profile: "FaultProfile", seed: int) -> "ToolBody": ...
def decide(rule: "FaultRule", seed: int, step_id: str, attempt: int) -> bool: ...   # pure

# aksha/evals/case.py (proposal; the first milestone's pytest cases use the same shape)
@dataclass(frozen=True)
class EvalCase:
    id: str                                            # "s6-change-recovery"
    kind: Literal["bad", "guard", "control"]           # D13-3
    source: str                                        # "the product requirements", "bug #12", "review 25 Nov, item 4"
    category: Literal["capability", "safety", "recovery", "limits", "change",
                      "isolation", "extension", "robustness"]
    agents: tuple[str, ...]
    world: str                                         # the test-fixture world
    extensions: tuple[str, ...] = ()                   # extra folders, such as the conformance agent's
    change_steps: tuple["ChangeStep", ...] = ()
    faults: str | None = None
    model: "ModelSpec" = "rule"                        # rule | scripted:<file> | replay:<store>#<run> | live
    users: tuple["UserScript", ...] = ()               # two for isolation
    crashes: tuple["CrashStep", ...] = ()
    budget: Mapping[str, int] | None = None
    stop_policies_off: bool = False                    # the control arm
    key: str = ""                                      # answer-key section
    criteria: tuple[str, ...] = ()
    needs_action: bool = True
    k: int | None = None

# aksha/evals/run.py and metrics.py (proposal; designed)
def run_case(case: EvalCase, *, k: int, seed: int, out: Path) -> list["EpisodeResult"]: ...
def grade(episode: "EpisodeRecord", key: "AnswerKey", graders: Sequence["Grader"]) -> "Verdict": ...
def pass_at_k(c: int, n: int, k: int) -> float: ...    # 1 - C(n-c, k) / C(n, k); NaN when n < k
def pass_hat_k(c: int, n: int, k: int) -> float: ...   # C(c, k) / C(n, k); NaN when n < k
def k_effective(results: Sequence["EpisodeResult"]) -> int: ...
def cost_per_pass(results: Sequence["EpisodeResult"], unit: Literal["steps", "tokens", "usd"]) -> float: ...
def failure_clusters(results: Sequence["EpisodeResult"]) -> list["FailureCluster"]: ...
def control_delta(treatment: Sequence["EpisodeResult"], control: Sequence["EpisodeResult"]) -> "ArmDelta": ...
```

`EpisodeResult`, `CriterionResult`, `FailureSignature`, `FailureCluster` and the `Grader` protocol keep 05e sections 11.2, 11.5 and 12.2's shapes, with `EpisodeResult` gaining `case_kind` and `case_source`.

Events it writes: none in the runtime core. Fakes and replay adapters return replies that the durable step executor records as ordinary `step.completed` events, with the fake's `rule` ID and no usage. `run.started` records the run mode and adapter class name, never configuration or keys, and the fault profile's digest when one is active. The eval runner writes `results.jsonl` and reports into its own output folder, never into the event store under test.

Projections it reads: The run view, the Task tree and stop decisions (for the invariants and the recompute check), the artifact view with lineage and citation spans (for state and citation criteria), the outbox and receiver ledgers (for "sent once"), the receipt index (ADR-P19), and the trace projection's step counts (ADR-P21).

## Stress test (25 Nov 2026)

Three design reviews in the three stress reviews walked the Research Agent (the Research stress review), the Patent Agent (the Patent stress review) and the contrast coding agent (the coding stress review) through this ADR on 25 Nov 2026. Nothing was built or run; each verdict is a reading of the interfaces. The verdict values are: fits; fits with agent-side code (the core is unchanged); needs a runtime change (generic, with no agent word in the core); missing primitive (no ADR owned it).

| Agent | Verdict as written | The scenario | What this ADR now says |
|---|---|---|---|
| Research | Fits | Two fixture worlds, a fault switch timing out COMP-05's pricing fetch, the duplicate-vendor case | Nothing new; a live web run has no answer key, so its evals are replay regressions only (D4 allows it). S2 adds the research cases |
| Patent | Fits | S6 is a `guard` case from the product requirements | S2 adds the open-card and two-source variants as cases written before their fixes |
| Coding | Needs a runtime change | Replay re-runs `pytest`, whose output varies, and mismatches at the first test run | S1 tool replay adapters |

Verdict after the amendments: fits for all three once S1 lands. S2's cases are first-milestone work for the rows the first milestone fixes; S1 is later.

Open for the maintainer: S3 (no lean; it needs a grader design).

## Sources (public)

1. Chen and others, "Evaluating Large Language Models Trained on Code" (2021); Evan Miller, "Adding Error Bars to Evals", arXiv 2411.00640 (2024); Zhong, Raghunathan and Carlini, "ImpossibleBench", arXiv 2510.20270 (abstract).
