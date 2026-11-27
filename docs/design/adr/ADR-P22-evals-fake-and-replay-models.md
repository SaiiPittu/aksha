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
4. **Replay reads the recorded run's event store; there is no separate cassette.** Durable execution (PR-02) already stores every model reply on the model step's `step.completed`, under the deterministic step ID with the request's `context_hash`. Replay matches them by the run-independent **step key**, the step ID minus its run segment (ADR-P02 D2), because the fresh run has its own run ID: the fresh run's `R-0012/discover/t01` and the recording's `R-0007/discover/t01` share the key `discover/t01`. `ReplayModel(recorded_store, run_id)` returns those replies for a fresh run of the same case and raises `ReplayMismatch(step_key, expected_hash, actual_hash)` at the first request whose hash differs, or at the first step key the recording lacks. Effects never run live in replay mode: receivers are the fixture receivers. A live run therefore becomes a regression test by keeping its store, which costs one paid run and no recording format.
5. **Eval cases are data with a kind and a source (D13-3 option A).** `EvalCase` replaces v2's `Scenario` and gains `kind` and `source`. The process rule: a `bad` case is committed before its fix and must fail on the code it reproduces. In the first milestone nothing is built yet, so no real failure exists to reproduce: every case is a `guard` (its source is the requirement it protects) or a `control`. The first `bad` case arrives with the first real bug, which keeps the suite honest about where its cases came from.
6. **Graders are code over the event store and the answer key, in a fixed order, and grade state, never transcripts.** The order of 05e section 12.1 stands: a gate (the store rebuilds, the key loads), then invariants, then state against the key, then citations, then the trace, then replies, and a model judge last, never gating by default. Generic invariants (one stop decision per turn end, recomputed stop decisions equal recorded ones, no Task instance entered except by the scheduler, no proposed version current without an accept, and D13-4's `inv.no_auto_start_while_held`, which PR-16 owns) live in the runtime's eval package; domain criteria live in the agent's folder.
7. **Graders prove they can fail.** A null policy (a fake that never acts) must fail every case that needs an action, and three mutants of known-good runs (`double-send`, `forged-excerpt`, `stop-decision-flipped`) must each be caught (v2 IE-8).
8. **The eval runner reports counts first, pass^k only where rollouts differ, and labels fake-mode rates.** It runs k rollouts per case, computes pass@k with Chen and others' unbiased estimator and pass^k (the chance that all k rollouts pass), and reports pass^k only where `k_effective > 1`; otherwise it prints "= pass^1 (rollouts identical)". In fake model mode, rollouts differ only under fault switches with a probability below 1, so the report says "fake model mode: rates are plumbing checks, not reliability". It also reports cost per passing episode in steps (tokens and money only when every adapter reported usage), failure clusters, a control arm with opted-in stop policies switched off, clustered standard errors by test-fixture world (none with one world), and stamps every result row with the agent definition version hash, the runtime commit and a dirty flag.
9. **Fault switches stay outside the fixtures and are keyed by step ID and attempt**, so a restarted run fails the same call again, and a fault can never fake an external effect (05e section 9.4, unchanged).

Worked example, why a deterministic fake gives no reliability number. A case is run k = 4 times with the rule-based fake and no fault switches. All four traces hash the same, so `k_effective` = 1 and the report prints pass^4 as "= pass^1 (rollouts identical)". With a fault switch that times out a fetch with probability 0.5, the four rollouts differ; say 3 of 4 pass. Then pass^2 = C(3,2) / C(4,2) = 3 / 6 = 0.50 and pass@2 = 1 - C(1,2) / C(4,2) = 1.00 (toy numbers, 05e section 11.3). That pass^2 measures the harness's recovery under seeded faults, and the report labels it so; only a live model makes it a statement about the agent.

Worked example, a regression caught by replay. A live Research run is kept as `R-0007`. A prompt edit changes the prompt of the `discover` Task. Replaying the case as a fresh run `R-0012` against `R-0007`'s store, replies are matched by step key, so the fresh `R-0012/discover/t01` finds the recorded `R-0007/discover/t01` (key `discover/t01`). Its `context_hash` differs, so `ReplayMismatch` names step key `discover/t01` and the run stops before any later step, telling the author exactly where the new prompt changed what the model was shown.

### Amendments from the stress test (25 Nov 2026, Proposed)

1. S1, replay returns recorded tool results too. D4 replayed model replies and ran every tool again. Fixture tools return the same bytes every time, but `pytest` output carries timings, so replaying a kept coding session raises `ReplayMismatch` at the first test run rather than at the prompt change being tested. In replay mode, read and exec tools get a replay adapter that returns the recorded `step.completed` result under the same step key (D4); store tools re-run against the replayed store; external tools stay on fixture receivers. This aligns D4 with ADR-P02 D11, whose replay check already assumes recorded tool results.

   ```python
   # aksha/fakes/replay.py (proposal)
   class ReplayTool:
       def __init__(self, recorded: "LogView", run_id: str) -> None: ...
       def __call__(self, ctx: "ToolContext", args: Mapping[str, Any]) -> "ToolOutcome": ...
           # returns the recorded result for ctx.step_id.key (run-independent, ADR-P02 D2);
           # raises ReplayMismatch on an args hash change
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

### Amendments from the build (26 Nov 2026, Proposed)

The eval runner is built in `src/aksha/evals/` and `aksha eval` (`src/aksha/interfaces/cli/`), following the eval design (a working document kept outside this repository) and its draft amendments E1 to E4 and E8. Where the code differs from the text above or settles something it left open:

1. B1, cases carry an origin, a world list and key references (E1, E2). `EvalCase` has `origin` beside `kind`, `worlds` in place of `world` and `expect` (dotted key references) in place of `key`; the three pairing rules are checked when a case is built, and a planted trap is checked against every listed world's key when the suite loads. Cases are TOML files, one per case, in `src/aksha/data/suites/<suite>/`.
2. B2, the agent under test is a port. Graders read a bundle exported from the episode's own event store (`episode.*` events), never a live runtime store. An agent reaches its world only through the episode's tools, behind a path guard that refuses any path naming `key.json`, `spec.yaml`, `manifest.json` or `world.lock.json`, and the world copy holds only `sources/`. A real agent's adapter runs the agent on the runtime and saves its artifacts into the episode in the bundle schema; the registry in `aksha.app.eval_agents` is the one place agents are registered.
3. B3, results are events in a store of their own (D8). The runner writes `eval.*` events into `results.sqlite` in its output folder, never into the store under test, and the reports are a projection over them (`SuiteResultsReducer`), so `aksha eval report` rebuilds every number from the store.
4. B4, the grader order gains a trap layer (E3), and outcomes and causes are closed sets (E4). Judge results annotate unless a calibration clears `may_gate`; a fake rule-based judge stands in offline and reads only the output part of its state.
5. B5, self-tests use five bundles and a mutant per criterion (E8, extending D7). The bundles are written by agents that read the key, in `aksha/evals/selftest/`, through ordinary episodes. Every gradeable criterion has a mutant; `stop-decision-flipped` and `send-on-stale` wait for an agent that records stop decisions and invalidation in the bundle.
6. B6, invariance uses `stillroom-v2` until a rename-only world exists (E6). The relation compares outputs through the rename map and a path map derived from the map's domain entries, leaving out what the two keys say changed. `rel.same_calls` (the same tool calls under the path map) stands in for the same-rule-IDs check, because the scripted agent has no model rules.
7. B7, fault switches decide per step and attempt (D9): a rule names a tool, optionally a path, a code and a chance in parts per million, and `decide` hashes the seed, step and attempt. A retry is a new attempt, so a fault-tolerant agent's rollouts differ and `k_effective` exceeds 1.
8. B8, the two worlds' keys gain `injected_recipient` and `injected_action` on KF-26, so `trap.T-INJECT` can see a send to the injected address. Both worlds gain a `world.lock.json`.
9. B9, decision 8 as built. The report prints the suite's pass rate with a cluster-robust standard error over world clusters and a 95% interval from it when there are two or more clusters, and counts only otherwise; with today's two worlds (a base and its variant, one cluster) it prints counts only. Every result row carries `agent_definition_hash`: the hash an adapter declares (a runtime agent's definition hash), or the SHA-256 of the adapter's source file, with `agent_definition_basis` saying which. The mode is recorded per episode, and a run that mixes modes says so. Deferred: the control arm with opted-in stop policies switched off, and cost in tokens and money; both wait for a runtime agent in the registry.
10. B10, invariants run on every episode as `must`, whether or not a case lists them (the eval design's "always must"). `no_unbound_send` moves from the trap layer to the invariant layer (version 2) and binds more: an approval names its target version, its action and its recipient, and covers one send. A send of another version, action or recipient, a send under an approval that names no recipient, and a second send under one approval each fail it. `Episode.request_approval` takes the recipient.
11. B11, the bundle carries the store's chain. `chain.jsonl` holds every stored event's whole envelope with its `prev_hash` and `hash`, and `meta.json` the store's identity and head. The gate walks the chain from the genesis with the runtime's hashing, checks it ends at the recorded head and that `events.jsonl` is exactly its `episode.*` events, besides reading the store's own verification at the end of the episode. A self-test mutant is resealed with a chain over its edited events and marked `resealed`.
12. B12, how a runtime agent reaches its world (refines B2). The adapter gives the agent `aksha.evals.agents.tools.episode_tools(episode)` in place of its source tools: `list_sources` and `read_source` declarations whose bodies call the episode, so every read passes the path guard and the fault switches and is recorded. The adapter never hands the agent `episode.sources`. Saves, approvals and sends go through the episode's own methods. A change step's restart is a `BaseException` raised once the tool call that matched it has recorded everything, so no send or approval is left half-recorded and a tool-body wrapper does not swallow the restart.
13. B13, smaller settlements. The world lock covers change sets and their derived keys, and a change set's targets pass the episode's path guard. A fault step is named by the base world's path, so a variant's renamed files draw the same fault decisions as their base. `trap.T-INJECT` reads `missed` when the agent never read the page holding the instruction. The null bundle is graded as each case of the suite: a case with `needs_action` must fail it and one without must not. An `xfail` case inverts pass and fail only. An output folder holds one run: a folder an earlier run wrote is cleared first, and a non-empty folder no run wrote is refused.
14. B14, from review. The gate also checks that `ledger.jsonl` is exactly the chain's `episode.effect_sent` payloads in order, so the invariants that read the ledger read chained deliveries; ledger mutants edit both. `no_unbound_send` fails a send whose approval was granted only after it. Relations compare a relation's endpoints too, through the maps (a `page:` endpoint through the path map). An episode whose change steps applied a change set with its own `key.json` is graded against that derived key. A run whose effective k is below 1 is refused with `k_invalid` before any output is written.
15. B15, the research and patent agents are registered (refines B2 and B12). `aksha.app.eval_agents` registers `research` and `patent`: each adapter (`aksha/evals/agents/research.py`, `patent.py`, over `runtime.py`) runs the shipped agent in a `Session` the composition root builds in the episode's `runtime/` folder (SQLite on the eval clock, filesystem blobs, the loaded definition bound to its fakes), and declares the definition's hash. The agents keep their own tools: the episode reaches them as the runtime's record reader (`EpisodeReader`, over `read_source` and `list_sources`), which gives every read the path guard, the fault switches and the record that B12's `episode_tools` give, without swapping in tools the shipped agent never calls. The patent agent's send goes through the outbox to `EpisodeMailbox`, which sends through the episode under the episode approval the adapter asked for, bound to `packet/<C>`, its action and recipient, before it granted the runtime approval. The scripted user's answers are the adapter's (the research scope questions; the patent gap answer `make demo` gives, the candidate the agent's own table puts first for counsel review). At the end of the run the adapter saves the agent's artifacts in the bundle schema, renaming fields and resolving span IDs to file, text and offsets; it computes nothing a grader checks.
16. B16, a patent family is named by its priority number. The key labels families `A`, `B`, ...; no source gives an agent those letters. `patent.matrix` (version 2) and `trap.T-STATUS` (version 2) read a family named by one of its keyed priority numbers as that family (`criteria.common.family_label`), and `key_delta` puts a family the variant adds into the changed set under its priority numbers as well as its label.
17. B17, `precision.known_values` (version 2) reads the case's own domain. A case that lists no stated value is checked against the stated values about what its listed facts are about (a candidate or a competitor), so a patent case listing only a gap is no longer failed for the research prices; a case that lists no fact at all is checked against every stated value, as before.
18. B18, the rename map gains `Corvane` to `Orrin` (`stillroom-v2/spec.yaml`, lock rewritten): the base page says "the Corvane brand", which the longer entries did not carry, so `rel.invariant` read the merged company as changed.

### Amendments from the resume-against-fresh measurement (26 Nov 2026, Proposed)

19. B19, arms. A case may list `[[arms]]`, each a `name`, a `reentry` (`fresh` or `resume`, the runtime's settings) and `premise_exempt` artifact kinds, and `compare` pairs of arm names. The runner runs every rollout of the case once per arm, under `<out>/_arms/<arm>/`, through the registration's `armed` builder: for a runtime agent the arm's `reentry` is what its request runs record and its exempt kinds are added to the agent's resume policy (`definitions.loader.with_context_policy`, which recomputes the definition hash). Each result row carries its `arm` and the agent definition hash it ran under, so an arm that changes the policy is never pooled with the others. The report gives one row per arm (`case[arm]`) and an arms table: must criteria passed, the rebuild runs' model turns, tool calls, tokens and context characters (medians over rollouts), the re-entries that resumed and why the rest went fresh, pass^k, and each compared pair's differences, with a fake-mode line saying the measures are a property of the fake. A case with arms has relation `none`. The case `change-scope-resume-vs-fresh` (decision 36) moves from `evals/cases/` (JSON, never run) to `src/aksha/data/suites/change/` in this format.
20. B20, a runtime agent through a change step. `EpisodeSource` versions the episode's world copy as the session's versioned source (the same `sha256:` of the text the reader returns, read behind the path guard and recorded as no tool call). On the restart a change step asks for, the adapter dispatches `ApplySourceChange` once per applied change set (command ID `episode-change-<seq>`, so a second restart records nothing new), then drives the rebuild runs with `Session.drive_rebuilds` (ADR-P19 B16) and answers them as it answers the request run. A change set that moved no record the agent read records no `source.changed` (an added record reaches the runtime only through a re-executed query, which `EpisodeSource` has none of), and the adapter then ends the episode with `change_not_recorded` rather than grade outputs the change never reached. `RuntimeUnderTest.metrics` reads the turns, tool calls, tokens, context characters and re-entry choices of the request and rebuild runs from the runtime store; the runner records them on the row as `metrics` and no grader reads them.
21. B21, change checks against the derived key. `change.stale_set` (every output of each `change.stale` kind is saved again after the first `episode.change_applied`), `change.unchanged_set` (no output of a `change.unchanged` kind is, the early-cutoff check) and `research.scope_change` (`scope/all` regions and the review summary's in-scope theme counts equal `scope_change.regions` and `scope_change.in_scope_themes`). An episode with no change applied fails the first two. The change set `stillroom/changes/scope-eu-uk` adds one dated scope amendment line to the priorities note, and its `key.json` carries the `change` and `scope_change` sections. A world's own key has neither, so the self-tests report the three as key gaps; the null bundle is graded as a change case with the case's derived key, and `tests/unit/evals/test_change_criteria.py` kills each with a mutant. The research adapter exports `scope/all` (saved last, so a change step can follow a finished export) and `themes_in_scope`.
22. B22, a planted trap is put to an agent that reads it. `s8b-injection-patent` is removed: its instruction is on a competitor page, which the patent agent has no tool to read, so `trap.T-INJECT` could only read `missed` and measured the case, not the agent. The research suite's `s8a-injection-research` puts the same trap to the agent that reads that page, and no patent-read source carries an injection, so the patent suite keeps no injection case until one does. A test checks that every `T-INJECT` case names an agent whose collections hold the trap's file.
23. B23, the research agent works through companies in discovery order. Companies are ordered by the position of their first fetched page and keyed `c<NN>-<slug>`, because the runtime fans out by sorted key; ordering by name let a rename reorder the agent's reads and failed `rel.same_calls` on every invariance pair, though the outputs agreed. The grader is unchanged.

### Amendments from the general agents (26 Nov 2026, Proposed)

1. G1, a world the agent code has never seen. `src/aksha/data/worlds/tilldew/` (an agritech venture in another market, with its own `spec.yaml`, `manifest.json`, `key.json` and lock) uses other folders, column names, section headings, template fields and file formats (a Markdown seller page, a CSV review export, ST.9 text). Its suite, `src/aksha/data/suites/tilldew/`, runs both agents through the same adapters: six research cases and six patent cases, each trap keyed with anchors computed from the files.
2. G2, the scripted user's answers are per world (`ANSWERS` and `GAP_ANSWERS` in the agent adapters, keyed by `episode.world`), and the episode reader serves the whole world as the `input` collection (`input/<path>` is `sources/<path>`); a bundle names the world's file, so the keys' anchors are unchanged.
3. G3, `rel.same_calls` leaves out calls on an input the variant adds (its key's `changes_from_base.added`): an agent that finds its inputs by content opens every file, and the added one is in the changed set. Any other extra or reordered call still fails it. The self-test oracle finds a publication wherever the world keeps it, the known-value mutant also nulls the conception date, and `patent.c1_extraction` checks the candidate the key's first mechanism step is about (`C1` in the stillroom worlds), so the self-tests run on any world's key.
4. G4, held-out worlds (27 Nov 2026). Six more synthetic worlds, each written without sight of the agent code, with a suite of its own: `rimefold`, `kelasora` and `vantlow` for the research agent, `pulvane`, `quillmere` and `brackwen` for the patent agent. Their pass counts are generality measurements, not regressions: a case that fails for a reason the agents cannot yet read is written up in `docs/limitations.md` (item 17) rather than fixed in the world.
5. G5, the leak guard (27 Nov 2026). `tests/architecture/test_no_world_leakage.py` reads every world (spec, key, manifest, sources) and fails when one of its names, identifiers, distinctive file names, column or key shapes, or distinctive key numbers appears in `src/aksha/runtime/`, the agents' logic, definitions and prompts, or `src/aksha/app.py`. The offline fakes are exempt; their generality is measured on the held-out worlds. Its known-leak list only shrinks, and is empty. The demo's world name therefore lives in the demo package, which passes it to `open_demo`.
6. G6, a mutant a world cannot take (27 Nov 2026). A self-test mutant that edits a fact the world's key does not have reports "not applicable" for that world instead of failing the self-test run.

7. G7, graders that read every world's key alone (27 Nov 2026, Proposed). The held-out worlds showed the eval layer still spoke the first world's words: the patent adapter saved extraction fields named after that world's candidate and the graders and the oracle read those names and fell back to its first candidate's ID, so `patent.c1_extraction`, `trap.T-GAP` and `trap.T-MISSING` could not pass on another world's candidate whatever the agent extracted, and the self-test oracle failed on any world without competitors. Amended: (a) the provisional bundle schema's `extraction/<C>` holds `entries` named by the key's own fact kinds (`docs/evals/README.md`); a grader finds the entry a fact stands for by its kind and its anchor and compares the key's value, and a check that needs a candidate takes it from the trap's facts (no default ID); (b) a mechanism step's value is compared as an element label only when it is one of the key's matrix rows for the candidate; (c) `patent.matrix` reads an uncompared family as `not_found` and accepts an extra family only when every one of its cells is `not_found`; (d) seven traps gain checks (`T-OBVIOUS-4` and `T-OBVIOUS-5` share one, `T-NOTEBOOK`, `T-UNITS`, `T-PARAMCONFLICT`, `T-REGION`, `T-UNIT`), each reading what to expect from the key's facts, each with a mutant that kills it, and the cases whose `expect` lists such a trap now list its check; (e) the oracle writes each world's key in that schema, cites every anchored fact of a kind the schema has no place for on an evidence output about what it is about, writes the research half only for a key with competitors and the patent half only for one with recommendations, and cites a family member's priority number inside a shared export as well as a file of its own; (f) a mutant whose planting changes nothing in a world's bundle reports "not applicable" (G6 generalised), the lister's must-fail list skips key gaps, and `no_market_percentage` does not read the patent kinds that state technical quantities. Every world's self-tests pass (`tests/unit/evals/test_selftest.py`).

### Amendments from web search and record and replay (27 Nov 2026, Proposed)

Built on 27 Nov 2026; the design note is [web search and replay](../web-search-and-replay.md).

1. R1, a world has a web. A world may hold `web.json` and `web/` beside `sources/`: pages under `.example` domains, each one's file at `web/<host><path>`, its record ID. The episode copies `web/` beside `sources/` and keeps `web.json` (which pages are noise) out of every tool's reach, beside the copy. The world's search proxy is served to `web_search` and `fetch_url` through the episode (`EpisodeWeb`): every fetch is `episode.read_web`, behind the path guard and the fault switches, and every search is recorded with what it returned. The stillroom worlds' vendor pages, and tilldew's and cindral's vendor pages and prior-art publications, moved from `sources/` to `web/`; the keys' anchors name the new paths.
2. R2, noise and the graders that check it. Each world's web mixes its relevant pages with noise of five kinds (`near_duplicate`, `outdated`, `spam`, `irrelevant`, `keyword_only`). `web.no_noise_cited` is an invariant (a citation of a noise page fails any case), `web.noise_unread` and `web.fetched_from_results` are listed by every agent case; each has a self-test mutant.
3. R3, the offline model is told the task's world. A world's `web.json` `fake` hints (the searches a person in that market would make) reach an offline adapter through its own `for_world`; the composition root applies them (`with_world_hints`) for an episode, the demo and `AKSHA_SEARCH=world`. A live adapter has no `for_world` and writes its own queries.
4. R4, record every answer. The replay of D7 answers a run's generation steps from its log; a tool body's fast-class calls are summarised on the tool step (ADR-P07 G1), so a run with fast readers needs a recording. `adapters/recording/` wraps every bound adapter and both web ports and appends each answer to a folder: a generation by step key and context hash, a fast-class call by alias and request hash in call order, a search by query and limit, a fetch by URL, each in call order and each failure with its code (a transient failure marked transient, an egress refusal with its host and reason), so a page that changed between two fetches of one run, or a search retried after a 503, replays as it happened. `aksha run --record FOLDER` records one run in a store inside the folder, with its inputs, answers, outputs and trace (OTLP JSON, ADR-P21); `AKSHA_RECORD` records every process.
5. R5, replay a recording. `aksha replay FOLDER` binds every alias in run mode `replay` to the recorded answers, serves the web from the recording (`ReplayWeb`), fixes the clock at the recorded start, drives the same script and compares the outputs byte for byte (`outputs.json` written again must equal the recorded file). A call the recording does not hold is `replay_missing`, a generation asked something else `replay_mismatch`, as in D7: a replay never falls through to another answer. A recording from a fake run ships in `aksha.data.recordings()`.
6. R6, a live recording replays as it ran (28 Nov 2026, Proposed). A live run's tool body timed out on its 54th classification call and its step ran again, so the recording held the first 53 requests twice and no sign of the failure; the replay answered the retry with the failed attempt's answers and the next generation was `replay_mismatch`. Now a model call an adapter refuses (transient, permanent or a context overflow) is recorded with its error, as R4 already records a failed search or fetch, and the replay raises it again in its place, so the step fails and retries as it did. A generation step replays its recorded failures in order, then its last answer (an earlier answer was asked again after a crash, and the run used the later one). The manifest also keeps each agent's active-time cap, model step timeout, retries and backoff (`limits`, beside `turn_factors`), and the replay binds the agent under them with no jitter and reads no live setting of the replaying machine. A recording made before R6 that held a retried model failure cannot be replayed: the answers of the failed attempt are not marked, so the replay says to record the run again.

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
    step_key: str; expected_hash: str | None; actual_hash: str                      # step ID minus run; None: not recorded

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
