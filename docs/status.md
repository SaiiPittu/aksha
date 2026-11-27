# Status: what is built, results and roadmap

The inventory, the eval results and the roadmap, as of 28 Nov 2026. The README keeps a summary, and [requirements.md](requirements.md) maps each requirement to its evidence.

## What is built

Counted from the code on this branch on 28 Nov 2026:

| | |
|---|---|
| Primitives | 29 of the 30 in the [catalogue](design/03-primitives.md) have code and tests; erasure (P30) is open: its options are listed and none is chosen |
| Agents | 2 (research, patent), each a data definition loaded and hashed by one loader; extension API 2.18 |
| Tools per agent | The harness declares the shared tools every agent lists: `list_inputs`, `read_document` and `search_inputs` over the uploads, `web_search`, `fetch_url` and `todo`. The research agent lists only those six; the patent agent adds six domain tools (publication search and fetch, two template readers, `fill_form`, `send_packet`) |
| Super agent | A supervisor Task over per-item worker Tasks, reading a capped work board: two tiers (triage for every item, deep work for the ones it admits), admission bounded in code, an audited sample of the drops with a reported miss rate. The patent agent runs under it above eight candidates (decision Proposed) |
| Model providers | Rule-based fakes offline (the default); live, any provider LiteLLM reaches, or any chat-completions server through a standard-library adapter |
| Session commands | 16 named commands behind one `dispatch`, with stored receipts so a retried command gets its first receipt back |
| Event types | 62 in the core catalogue |
| Command line | 24 subcommands, one server (`aksha serve`, which also serves the web app with `--web`), a terminal chat (`aksha chat`), fixed exit codes |
| Tests | 3,720 collected (unit, contract, integration, architecture, agents), offline; `make test` runs them on every core in about 6 minutes; the web app has its own Vitest and Playwright tests in `web/` |
| Evals | 13 suites over 11 fixture worlds |

The pieces around invalidation: a policy gate in code (deny by default, approvals bound to the exact action by signature), durable execution that resumes after a crash, a transactional outbox keyed by meaning so a retry never sends twice, model classes (extraction, classification, generation) with confidence thresholds, grounding that checks every quote at save, a correction loop (a person or an agent proposes a fix with evidence, the domain owner approves it, invalidation rebuilds what read the old value), cross-run reuse of outputs whose reads still hold, and tracing as a projection of the store. The tiers, ports and dependency rule are in the [architecture overview](architecture/overview.md); the architecture tests in `tests/architecture/` enforce them.

On the 20-candidate `coldwick` world the supervised patent run takes 138 model turns, against about 196 for deep work on every candidate, and the audit catches the one wrong drop planted in the world (ADR-P15 S5). Those are fake-model numbers.

## Results on the held-out worlds

Six of the eleven worlds were written without sight of the agent code, to test whether the offline agents generalise beyond the fixtures they were built on. Every suite was run with `aksha eval run <suite>` (`--k 3` for `change`), in fake model mode, on main (`f8b8f58`) on 27 Nov 2026 and again on 28 Nov 2026 after the web app and derived-counts changes merged, with the same results:

| Suite | Agent | Written blind | Cases passed | Grader self-tests |
|---|---|---|---|---|
| research | research | no | 6 of 6 | pass |
| patent | patent | no | 8 of 8 | pass |
| scripted | scripted test agent | no | 7 of 7 | pass |
| change | research | no | 1 of 1 (3 arms, k = 3) | pass |
| tilldew | both | no | 12 of 12 | pass |
| cindral | both | no | 11 of 11 | pass |
| rimefold | research | yes | 7 of 8 | pass |
| kelasora | research | yes | 9 of 9 | pass |
| vantlow | research | yes | 9 of 9 | pass |
| pulvane | patent | yes | 7 of 8 | pass |
| quillmere | patent | yes | 3 of 7 | pass |
| brackwen | patent | yes | 7 of 9 | pass |
| portfolio | patent, under the supervisor Task | no | 1 of 1 | pass |

On the held-out worlds the research agent passes 25 of 26 cases and the patent agent 17 of 24 (3 of 23 before its reader and the graders were made general; `pulvane` gained a case). What still fails is the offline comparison, which counts a step's words where the worlds' authors judged paraphrases, and one threshold stated in other units; [limitations item 19](limitations.md) lists each gap, and says why the three patent worlds no longer count as held out. They are left failing on purpose rather than fixed with a rule that fits one world. These are fake-model numbers, so they test the harness and the offline rules, not a model. Every suite's grader self-tests pass, and no suite had an invariant failure.

## Roadmap

| Item | Status on 28 Nov 2026 |
|---|---|
| Citation-keyed invalidation, scoped absence claims, early cutoff | Built; opt-in per agent, both shipped agents opt in |
| Definitions as dependencies: an extraction schema is a strong dependency, and the agent definition hash pins the modules it imports | Built; a dynamic import by a computed name is not seen by the hash |
| Correction loop: propose with evidence, owner approves, rebuild what read the old value | Built (decision Proposed); no tool lets an agent propose a correction inside its own run, and nothing checks the approver's role |
| Cross-run reuse of outputs whose reads still hold | Built; `AKSHA_REUSE=off` turns it off |
| Shared input tools: the harness declares the upload, web and plan tools, agents keep only their domain actions | Built (decision Proposed) |
| Super agent: a supervisor Task over a work board, two tiers, an audited drop list | Built as a slice (decision Proposed); the patent agent uses it above eight candidates. The scale path (its instructions, criteria changes, an incremental board, pivot ranking) is not built |
| Live models from any provider through LiteLLM | Built and smoke-tested on one Gemini model; native file input is in the code but off |
| A clean failure for a Task that fails while awaiting rebuild ([#95](https://github.com/SaiiPittu/aksha/issues/95)) | Built (decision Proposed); a later decision can still re-enter it while other work runs |
| Erasure in the append-only store (P30) | Open: options listed, none chosen |
| A whole task on a real model, the evals in live mode, and the live fresh-against-resumed rebuild comparison | Not run |
| A container backend, a real system of record behind the outbox, OTLP/HTTP export | Not built |
| Unattended operation: stronger isolation, a halt-everything stop, per-agent identity, an untrusted-origin mark, an approval inbox, event-triggered runs (X-1 to X-7) | Designed, not built; see [future directions](design/05-future-directions.md) |

| A web app and a terminal chat over the session API, with accounts, orgs and roles | Built ([#101](https://github.com/SaiiPittu/aksha/pull/101)); accounts are in the HTTP layer only |
| The patent agent reading research outputs by artifact ID and version | Not built: the loader supports it, the patent agent declares no such read ([requirements R-36](requirements.md)) |
