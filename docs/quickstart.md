# Quick start: the demo and the command line, step by step

*As of 26 Nov 2026. The shortest path is in the [README](../README.md): `aksha demo`, then `aksha example` and three commands. This page is the long walk-through from a clone, every command with the output it printed. Nothing here calls a model over the network: every model is a rule-based fake or a replay.*

## From a clone

`make venv` downloads the dependencies once; after that everything below runs offline. There are no API keys and no model calls over the network: every model is a rule-based fake or a replay of a recorded run. Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are needed.

```sh
make venv    # .venv with aksha (editable), dev tools, the server and the trace exporter
make test    # the full suite on SQLite, offline
make demo    # the change-and-recovery demo, about 20 seconds (creates .venv itself if missing)
```

`make demo` tells the change-and-recovery story in one command:

1. The research agent and the patent agent run to completion on the synthetic `stillroom` world. The patent agent sends one invention's counsel-review packet to a mock mailbox after an approval.
2. The input changes to the `stillroom-v2` change set: a new publication family, and an earlier date on an outside suggestion.
3. Invalidation reopens only the work that read the moved records. In the run on 26 Nov 2026 it rebuilt 10 patent outputs and kept the other 13 and all 11 research outputs. The packet's diff review is accepted and a fresh approval is asked for the new version.
4. The new packet is sent by a child process that kills itself with a real `SIGKILL` after the mailbox stores it and before the runtime records the receipt. On restart, recovery asks the mailbox by idempotency key and does not send again: 2 deliveries for 2 keys, one per packet version.
5. The event store's hash chain is verified.

Every answer and approval a person would give is scripted and recorded with `by="demo-script"`; the store and the mailbox stay in `var/demo/`.

## Your own files

The research agent and the patent agent work on whatever you give them: a folder or a set of files (saved web pages, Markdown notes, CSV or JSON exports, emails, PDFs, Word, PowerPoint and Excel files, images; the binary formats need `pip install "aksha-harness[ingest]"`, which `make venv` includes). The request says what to research, for whom and what to compare; the agents find each input by its content, never by its name or folder. The example below uses the synthetic `tilldew` world (an agritech venture; see [src/aksha/data/README.md](../src/aksha/data/README.md)) as if it were your folder. Run it from a fresh clone, after `make venv`:

```sh
. .venv/bin/activate
export AKSHA_HOME=var        # keep this walk-through's state in ./var (an install uses the per-user data folder; aksha doctor shows it)
export AKSHA_SEARCH=world:tilldew   # the world's offline web: its vendor pages and prior art, with noise
cp -r src/aksha/data/worlds/tilldew/sources my-inputs     # stands in for your own folder

aksha run --agent research --input my-inputs "Research the competitors for Tilldew's solar soil-moisture probe, for smallholder vegetable farmers in India. Compare power (solar, battery, AA, cells), battery life (battery life, years of battery) and connectivity (SMS, 2G, LoRa, gateway). What do farmers say about: accuracy (accura*, wrong reading*, false alert*, still wet, trust*); connectivity (sms, signal, network, gateway); price (price*, expensive, cost*, cheap*, paid)?"
# uploaded 14 input files
# session S-...
# R-0001 research ended complete                                          (exit code 0)

aksha show                   # every latest output: research.matrix@v1, ...; stale ones marked
aksha show research.needs    # one output's content as JSON, every value cited
```

Uploaded files become versioned records of your inputs (`var/inputs/<user>/`). Upload the folder again after changing a file, and only the work built on that file reopens:

```sh
aksha upload my-inputs       # nothing changed
# uploaded 14 input files
# no work reopened: no input changed, or none that work was built on
sed -i 's/Too expensive for a two-acre plot./Too expensive, and the battery died./' my-inputs/exports/farmer-reviews.csv
aksha upload my-inputs
# uploaded 14 input files
# R-0002 research ended complete                                          (exit code 0)
aksha inspect R-0002         # its steps: only the review reading (voice) and the report re-ran
```

The farm's own files are the uploads; the vendors' pages and the published prior art are on the web the agents search (`web_search`, `fetch_url`), here the world's offline web, which mixes them with noise: a copy of a vendor page on a listing site, a sale site, a glossary, a skin-moisture probe, a mirror of a publication. The agents search, choose what to fetch and cite only what they fetched; `aksha show research.competitors` lists the queries, what was fetched and what was skipped, and why. See [web search and replay](design/web-search-and-replay.md).

The patent agent reads the same inputs (invention notes, contributor and disclosure tables, emails, priorities, blank forms) and searches the web for prior art, and prepares a counsel-review packet:

```sh
aksha run --agent patent "Prepare Tilldew's invention candidates for counsel review."
#     asks: TD-2: the note leaves Tariff table source undecided. What is it?   (exit code 3)
aksha answer I-0001 "The state utility's published time-of-day tariff table."
#     asks: Which candidate should be prepared for counsel review? TD-1: prioritize_counsel_review; TD-2: collect_more_evidence; TD-3: collect_more_evidence
aksha answer I-0002 TD-1
#     packet: patent.packet/TD-1@v1                                       (exit code 4)
```

The approval and the mock mailbox that receives the packet work as in the stillroom walk-through below. The offline models understand a small grammar in the request: `Compare <dimension> (<terms>), ...` names the matrix's dimensions besides price (a term ending in `*` is a prefix, `not <phrase>` excludes text), `say about: <theme> (<terms>); ...` names the review themes, `for <segment>` and a country or region fix the segment and the geography, and a missing one is asked for. A live model reads the request as free text; see the limitations.

## How the agents read what you upload

Nobody describes your files in advance, so no single extraction schema fits them. Each read tool runs three steps through the agent's fast models (ADR-P07 G3), every one recorded on the tool's step:

1. **Decide.** A classification model names the record's kind from a preview code renders from structure alone (name, format, headings, header fields, a table's columns and sample values, the start of the text). The research agent's kinds are a competitor page, a pricing table, customer reviews, a market report and an internal note; the patent agent's are an invention disclosure, meeting notes, a contributor record, a disclosure record, an email, business priorities, a blank form and a prior-art publication. Anything else, or an answer the thresholds do not accept, is `other`.
2. **Pick the schema.** Each kind names the extraction alias whose typed fields are its schema (`text`, `number`, `date`, `time`, `datetime`, `duration`, `list`, `estimate`, `forecast`).
3. **Extract.** The model fills the schema; every value comes back with the exact words it was read from, which the runtime mints as a citation span and verifies verbatim when an artifact cites it.

What the fast models cannot read, a record of kind `other` or a field they abstained on, goes to the Task's general model: the tool returns the record's passages, each citable, and says what is `unresolved`. The kinds are declared data in each agent (`reading.KINDS`, `world.KINDS`), so adding one is a kind and its alias.

## Running the agents live, with your own key

Offline, every model is a rule-based fake (`AKSHA_MODE=fake`, the default). `AKSHA_MODE=live` binds every model alias to a live adapter. With one general model (any server that speaks the chat-completions shape, hosted or on your machine), it fills the generation contract and, in adapted mode, the decide and extract contracts too: the same pipeline and the same citation checks, its quotes located in code, recorded `native=False`.

```sh
export AKSHA_MODE=live
export AKSHA_LIVE_BASE_URL=https://your-provider.example/v1   # or http://127.0.0.1:8000/v1
export AKSHA_LIVE_API_KEY=...        # read from the environment only; never recorded
export AKSHA_LIVE_MODEL=your-model   # one model for every alias, or set per class or alias:
# export AKSHA_LIVE_MODEL_GENERATION=a-large-model
# export AKSHA_LIVE_MODEL_CLASSIFICATION=a-small-model
# export AKSHA_LIVE_MODEL_PAGE_FACTS=a-model-for-one-alias
aksha run --agent research --input my-inputs "Who sells ... and at what price?"
```

A native System 1 service (an extraction or classification API with its own spans and probabilities) is named per alias with `AKSHA_ALIAS_<ALIAS>=module:Class`, a class built as `cls(alias, env)` that implements the class's contract. The start is refused before any call, naming the setting to set, when the server or a model is missing (`live_config_missing`) or a server not on this machine has no key (`live_key_missing`). `AKSHA_LIVE_SAMPLES=N` asks each classification N times and uses the votes as its distribution. `tests/integration/test_live_mode.py` runs the research agent this way against a stub server on localhost.

## Record a run, replay it offline

`aksha run --record FOLDER` runs one agent to its end in a store of its own inside the folder and records everything the run was given: the message, the uploaded inputs, the answers (`--answer ID=TEXT`, `--approve`), every model answer (each generation, and each fast-class call a tool made), every search and every fetch, then the outputs and the trace (OTLP JSON). `aksha replay FOLDER` runs the same agent again from the recording alone, with no model, no search API and no network, and says whether the outputs are the same:

```sh
aksha example stillroom stillroom-inputs
AKSHA_SEARCH=world:stillroom aksha run --agent research --input stillroom-inputs --answer segment=offices --answer geography=EU --record runs/first "Research the competitors for the venture's occupancy sensor."
# recorded R-0001 research (complete) into runs/first: 173 model calls, 2 searches, 15 fetches
aksha replay runs/first
# replayed R-0001 offline from runs/first (complete) in runs/first-replay
# outputs: the same as recorded (11 outputs)
```

A live run is recorded the same way: set `AKSHA_MODE=live` with the `AKSHA_LIVE_*` settings above, and `AKSHA_SEARCH=brave` with `AKSHA_BRAVE_API_KEY` (read from the environment, never recorded) and `AKSHA_NETWORK=on`, so each page is fetched over HTTPS under the `aksha.web` egress policy. The replay needs none of them. A small recording made from a fake run ships in `src/aksha/data/recordings/research-occupancy/`: `aksha replay src/aksha/data/recordings/research-occupancy --out replay-1`.

## The command line, step by step

The research agent runs end to end through `aksha run` on the synthetic `stillroom` world, here configured as a folder every run reads (`AKSHA_SOURCES`) instead of uploaded. Run this from a fresh clone, after `make venv`:

```sh
. .venv/bin/activate
export AKSHA_HOME=var        # keep this walk-through's state in ./var (an install uses the per-user data folder; aksha doctor shows it)
rm -rf var                   # an empty store, so the IDs below match
export AKSHA_SOURCES=inputs=src/aksha/data/worlds/stillroom/sources   # a configured inputs folder
export AKSHA_SEARCH=world:stillroom   # the world's offline web: the vendors' pages, with noise

aksha run "Who sells camera-free occupancy sensing for offices, and at what price?" --agent research
# session S-...
# R-0001 research waiting
#   waiting for input (question) I-0001: aksha answer I-0001 TEXT
#     asks: Which geography should this cover? Prices and reviews can come from several ...  (exit code 3)

aksha answer I-0001 EU       # the agent asked which geography to cover
# R-0001 research ended complete                                          (exit code 0)

aksha status                 # where every run stands, read from the event store
aksha inspect R-0001         # the run inspector: budget, steps, stop decisions, as JSON
aksha verify                 # chain ok: <n> events, head sha256:...
aksha trace R-0001           # var/traces/R-0001/1.otlp.jsonl, one file per execution segment

aksha run "Who sells camera-free occupancy sensing for offices, and at what price?" --agent research --headless
# refused: sandbox_required (...)                                         (exit code 7)
AKSHA_SANDBOX=bwrap aksha run "Who sells camera-free occupancy sensing for offices in the EU, and at what price?" --agent research --headless
# R-0002 research ended complete (headless)
```

`AKSHA_SANDBOX` chooses the sandbox backend, which is probed once at start: `bwrap` (bubblewrap, Linux), `e2b` (the E2B cloud, with `E2B_API_KEY` set), `auto` (the strongest local one that probes available, never a cloud one), `refuse` (runs nothing) or `none` (no backend). Each session keeps one sandbox for all its runs (decision 37): with bubblewrap, a folder under `<AKSHA_HOME>/sandbox/bwrap/<user>/<session>/work` bound as `/work`, started from `AKSHA_WORKSPACE` when it is set (a git repository becomes a worktree on the branch `aksha/<user>/<session>`, with a checkpoint commit after each exec that changed it; your checkout is never touched). `aksha sandbox list` shows your session workspaces and `aksha sandbox clean <session>` removes one, keeping a git workspace's branch. A backend that does not probe available becomes `refuse` with the reason, never a weaker box, so on a host without bubblewrap the headless run above is refused. `<name>:<strength>` with any other name declares an outer box the operator attests the process runs in, such as a container: a headless run may start, and exec tools are still refused. The choice and every probe are recorded on `run.started`. Tools reach the network only through the runtime's HTTP client, under their egress policy, and only with `AKSHA_NETWORK=on`. With `--agent` left out, the router picks the agent from the loaded agents' purposes. State lives in `var/`; delete it to start again.

The patent agent runs end to end the same way, up to an approved send. Every waiting line says what is being asked:

```sh
rm -rf var                   # an empty store, so the IDs below match
export AKSHA_SOURCES=inputs=src/aksha/data/worlds/stillroom/sources

aksha run "Assess the four invention candidates and prepare the chosen one for counsel." --agent patent
#   waiting for input (missing detail) I-0001: aksha answer I-0001 TEXT
#     asks: C2: the note leaves Minimum distance from a vent undecided. What is it?   (exit code 3)
aksha answer I-0001 "0.5 m from the vent edge"
#   waiting for choice (candidate selection) I-0002: aksha answer I-0002 TEXT
#     options: C1, C2, C3, C4                                              (exit code 3)
aksha answer I-0002 C1
#   waiting for approval A-0001: aksha approve A-0001 --signature v1:...
#     approves: send_packet
#     packet: patent.packet/C1@v1
#     recipient: counsel-primary                                           (exit code 4)

aksha approve A-0001 --signature v1:<the first 12 or more digits shown>
# refused: system_unavailable (...)                                       (exit code 7)
```

Nothing is set up to receive the packet by default, so the approval is refused `system_unavailable`: nothing is approved, the run keeps waiting on the same approval, and your other commands go on working. For development, bind the patent agent's `counsel_mail` to the mock mailbox `make demo` uses, a JSON file that delivers once per key and reaches no one; every command then says it is a mock:

```sh
export AKSHA_SYSTEMS=counsel_mail=mailbox:var/mock-counsel
aksha approve A-0001 --signature v1:<the first 12 or more digits shown>
# mock: counsel_mail is a development mailbox at var/mock-counsel/mailbox.json (AKSHA_SYSTEMS); nothing it receives leaves this machine
# R-0001 patent ended complete                                            (exit code 0)
```

`var/mock-counsel/mailbox.json` then holds one delivery; approving again is refused `already_decided` and delivers nothing.
