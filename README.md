# Aksha

[![CI](https://github.com/SaiiPittu/aksha/actions/workflows/ci.yml/badge.svg)](https://github.com/SaiiPittu/aksha/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)

Aksha is a headless agent harness in Python. One runtime runs many agents, each defined as data in its own module, and every fact any of them produces lands in one append-only, hash-chained event store.

[Quick start](docs/quickstart.md) · [Architecture](docs/architecture/overview.md) · [Interfaces](docs/interfaces.md) · [Evals](docs/evals/README.md) · [Limitations](docs/limitations.md) · [Design](docs/design/README.md)

## The problem

An agent read 20 documents and wrote a decision. Then one of the documents changed. Is the decision still valid?

Most agent stacks cannot answer, because nothing recorded what the decision rested on. The only safe move is to run everything again, which costs time and money, and a person who already reviewed the old answer has to review all of it again. Aksha treats agent work the way a build system treats files: every output records what it read, and a change rebuilds only what depends on it.

## How Aksha answers it

1. **Recorded reads with versions.** Every tool read is a fact in the event store: which record, which version, which span of text came back. Every saved output (an artifact version) carries the list of reads it was built from. Staleness is computed from those facts, never stored as a flag.
2. **Citation-keyed invalidation.** A value an agent saves must quote a span a tool actually returned, and the runtime checks the quote at save. An agent can then key invalidation on its citations (`invalidation_keys="citations"`): its output reopens when a span it cites changes, or when the schema it was extracted under changes. A change elsewhere in a document it read is a weak dependency: the output stays current, is listed as noticed, and gets a low-priority recheck.
3. **Scoped absence claims.** "Nothing was found" is a claim too, so it must name its scope: the sources it covered or the search it ran. If that search later finds something new, or a record in its scope moves, the claim reopens.
4. **Early cutoff.** When a rebuilt step comes out equal to its old version, or a re-run search finds the same hits, the cascade stops there and nothing downstream of it rebuilds.

A machine-only output rebuilds by itself. An output a person reviewed comes back to them as a diff, and only their accept makes the new version current. The design follows the dynamic-dependency formulation in Mokhov, Mitchell and Peyton Jones, "Build systems à la carte" (ICFP 2018), and the decision is [ADR-P05](docs/design/adr/ADR-P05-invalidation.md).

In the demo below, the same change reopens 14 of the 34 saved outputs when keyed on citations, and 27 when keyed on every read.

## Quick start

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). No API key, and no network after the install: offline, every model is a rule-based fake.

### One command: the demo

```sh
uvx --from git+https://github.com/SaiiPittu/aksha aksha demo
```

Two agents (research and patent) run on a synthetic world, one input set changes, only the dependent work is rebuilt, and a send to a mock mailbox survives a real `SIGKILL`. An excerpt from a run on 27 Nov 2026 (about a minute here; the timings depend on the machine):

```text
1. Both agents run on the stillroom world (21.05 s)
   research R-0001 complete: 11 outputs, 22 model calls
   patent   R-0002 complete: 23 outputs, 43 model calls
3. Invalidation reopens only what depends on the change (18.99 s)
   strong, reopened: needs (scoped record); C1: extraction (cites nothing, so read-keyed); C1: prior_art (cites nothing, so read-keyed)
   weak, noticed and not rebuilt: patent.portfolio read reviews.json outside what it rests on; 1 low-priority recheck item(s)
   keyed on every read instead, the same change would reopen 27 outputs, not 14
   rebuilt 14: C1: comparison, extraction, handoff, packet, prior_art, recommendation; ...; findings, matrix, needs, tests, selection, table
   kept 13 patent outputs: ...
   kept 7 of 11 research outputs: ...
   early cutoff: the 10 queries that found the same hits stopped the cascade; 0 rebuilt version came out equal
4. SIGKILL mid-send, then recovery (7.02 s)
   at restart: 1 outbox entry open with no receipt; recovery asked the mailbox by key 1 time, sent 0 times
   mailbox: 2 deliveries for 2 idempotency keys (patent.packet/C1@v1, patent.packet/C1@v2): each packet version exactly once
5. Event store
   hash chain verified: 1499 events, head sha256:e70bde41c57a27cf...
```

The command exits 0 only when every check passes.

### Install

```sh
uv tool install git+https://github.com/SaiiPittu/aksha
# or: pipx install "aksha-harness @ git+https://github.com/SaiiPittu/aksha"
```

The distribution is `aksha-harness`; the command and the import are `aksha`. (`aksha` on PyPI is an unrelated project, so never `pip install aksha` or a bare `uvx aksha`.) Optional extras: `[server]` for `aksha serve`, `[otel]` for `aksha trace`, `[postgres]` for the PostgreSQL event store, `[ingest]` for PDF, Office and email parsing. `aksha doctor` prints the version, where state is kept and the install command for any missing extra.

### Run an agent on your own files

`aksha example` copies a bundled synthetic world to try it on. `--input` uploads a folder, and the quoted text is the request:

```sh
aksha example stillroom my-inputs
export AKSHA_SEARCH=world:stillroom   # the world's offline web: vendor pages mixed with noise
aksha run --agent research --input my-inputs "Who sells camera-free occupancy sensing for offices, and at what price?"
#   waiting for input (question) I-0001: aksha answer I-0001 TEXT
#     asks: Which geography should this cover? ...                       (exit code 3)
aksha answer I-0001 EU
# R-0001 research ended complete                                          (exit code 0)
aksha show                    # every saved output
aksha show research.matrix    # one output as JSON, every value cited
aksha verify                  # check the hash chain
```

Upload the folder again after editing a file and only the work that depends on the edit reopens. The patent agent, approvals, corrections, headless runs and the exit codes are in the [quick start](docs/quickstart.md).

### Run the evals

```sh
aksha eval list               # suites, cases and registered agents
aksha eval run research       # grader self-tests, then every case, graded from the episode's event store
```

Each case runs in a fresh copy of a fixture world, and the agent is graded on what it left in its event store, never on what it says it did. How to write a case and plug in an agent: [docs/evals/README.md](docs/evals/README.md).

### Record a run, replay it offline

```sh
aksha run --agent research --input my-inputs --answer geography=EU --record runs/first "Who sells camera-free occupancy sensing for offices, and at what price?"
# recorded R-0001 research (complete) into runs/first: 174 model calls, 3 searches, 15 fetches
aksha replay runs/first
# outputs: the same as recorded (11 outputs)
```

`--record` keeps the request, the inputs, the answers, every model answer, every search and every fetch, then the outputs and an OpenTelemetry trace. `aksha replay` runs the same agent from the recording alone: no model, no search API, no network. A call the recorded run did not make is refused, never answered.

## What is built

Counted from the code on main on 27 Nov 2026:

| | |
|---|---|
| Primitives | 29 of the 30 in the [catalogue](docs/design/03-primitives.md) have code and tests; erasure (P30) is in review |
| Agents | 2 (research, patent), each a data definition loaded and hashed by one loader; extension API 2.14 |
| Session commands | 16 named commands behind one `dispatch`, with stored receipts so a retried command gets its first receipt back |
| Event types | 62 in the core catalogue |
| Command line | 23 subcommands, one server (`aksha serve`), fixed exit codes |
| Tests | 3,244 collected (unit, contract, integration, architecture, agents), offline |
| Evals | 12 suites over 10 fixture worlds |

The pieces around invalidation: a policy gate in code (deny by default, approvals bound to the exact action by signature), durable execution that resumes after a crash, a transactional outbox keyed by meaning so a retry never sends twice, model classes (extraction, classification, generation) with confidence thresholds, grounding that checks every quote at save, a correction loop (a person or an agent proposes a fix with evidence, the domain owner approves it, invalidation rebuilds what read the old value), cross-run reuse of outputs whose reads still hold, and tracing as a projection of the store. The tiers, ports and dependency rule are in the [architecture overview](docs/architecture/overview.md); the architecture tests in `tests/architecture/` enforce them.

## Results on the held-out worlds

Six of the ten worlds were written without sight of the agent code, to test whether the offline agents generalise beyond the fixtures they were built on. Every suite run on a fresh clone of main on 27 Nov 2026, in fake model mode:

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

On the held-out worlds the research agent passes 25 of 26 cases and the patent agent 17 of 24 (3 of 23 before its reader and the graders were made general; `pulvane` gained a case). What still fails is the offline comparison, which counts a step's words where the worlds' authors judged paraphrases, and one threshold stated in other units; [limitations item 19](docs/limitations.md) lists each gap, and says why the three patent worlds no longer count as held out. They are left failing on purpose rather than fixed with a rule that fits one world. These are fake-model numbers, so they test the harness and the offline rules, not a model. Every suite's grader self-tests pass, and no suite had an invariant failure.

## Live mode

Offline is the default (`AKSHA_MODE=fake`). To bind every model to a chat-completions server, hosted or on your machine:

| Setting | Meaning |
|---|---|
| `AKSHA_MODE=live` | turn live mode on |
| `AKSHA_LIVE_BASE_URL` | the server, for example `http://127.0.0.1:8000/v1` |
| `AKSHA_LIVE_API_KEY` | the key; read from the environment only and never recorded (optional for a server on this machine) |
| `AKSHA_LIVE_MODEL` | one model for every alias |
| `AKSHA_LIVE_MODEL_<CLASS or ALIAS>` | a model per class or alias, such as `AKSHA_LIVE_MODEL_GENERATION` |
| `AKSHA_LIVE_SAMPLES` | ask each classification N times and use the votes (default 1) |
| `AKSHA_LIVE_TIMEOUT_S` | per-call timeout in seconds (default 60) |
| `AKSHA_ALIAS_<ALIAS>=module:Class` | a native extraction or classification service for one alias |
| `AKSHA_SEARCH=brave`, `AKSHA_BRAVE_API_KEY`, `AKSHA_NETWORK=on` | live web search, and page fetches under the `aksha.web` egress policy |

A missing server, model or key is refused before any call, naming the setting to set. Live mode is tested end to end only against a stub server on localhost; see the limitations.

## Limitations

The ones a first user meets, as of 27 Nov 2026:

1. **No real model has been run through the agents.** Every result above comes from rule-based fakes, so it shows the harness working, not model quality. Every confidence threshold is unfitted.
2. **The only system of record is a mock.** An approved send goes to a development mailbox, a JSON file that reaches no one.
3. **One local sandbox backend, Linux only.** Bubblewrap is the only local confining backend, and a session's sandbox is stateful: a crash loses what was only in its temporary space.
4. **Erasure is open.** Forgetting a memory hides it from recall but erases nothing from the append-only store; the erasure design is in review.
5. **Some reopening is still wider than the change.** An output that cites nothing stays keyed on every read, a changed seller page reopens every company's evidence, and in the patent agent an edit to one section of a record reopens all the work built on that record.

The full list, with what each one means in practice, is in [docs/limitations.md](docs/limitations.md).

## Roadmap

| Item | Status on 27 Nov 2026 |
|---|---|
| Citation-keyed invalidation, scoped absence claims, early cutoff | Built; opt-in per agent, both shipped agents opt in |
| Definitions as dependencies: an extraction schema is a strong dependency, and the agent definition hash pins the modules it imports | Built; a dynamic import by a computed name is not seen by the hash |
| Correction loop: propose with evidence, owner approves, rebuild what read the old value | Built (decision Proposed); no tool lets an agent propose a correction inside its own run, and nothing checks the approver's role |
| Cross-run reuse of outputs whose reads still hold | Built; `AKSHA_REUSE=off` turns it off |
| Erasure in the append-only store (P30) | In review |
| A live-model run of the agents and of the fresh-against-resumed rebuild comparison | Not run |
| A container backend, a real system of record behind the outbox, OTLP/HTTP export | Not built |
| Unattended operation: stronger isolation, a halt-everything stop, per-agent identity, an untrusted-origin mark, an approval inbox, event-triggered runs (X-1 to X-7) | Designed, not built; see [future directions](docs/design/05-future-directions.md) |

## Documentation

- [Quick start](docs/quickstart.md): every command with the output it printed
- [Architecture overview](docs/architecture/overview.md) and [codebase structure](docs/architecture/codebase-structure.md)
- [Interfaces](docs/interfaces.md): the session API, the server, the command line and tracing
- [Evals](docs/evals/README.md): running, writing cases, plugging in an agent
- [Design](docs/design/README.md): the primitives, the decision records and the decisions log
- [Limitations](docs/limitations.md)

## Development

```sh
make venv            # .venv with aksha (editable), the dev group and the optional extras
make check           # lint, type check and the full test suite on SQLite, offline
make test-postgres   # the PostgreSQL adapter's tests (make venv-postgres first)
make demo            # the same demo, state in var/demo/
```

Contributing and the test layout: [CONTRIBUTING.md](CONTRIBUTING.md). Releasing: [docs/releasing.md](docs/releasing.md).

## License

No license has been chosen yet. Until one is, the code is published for reading, and no license is granted.
