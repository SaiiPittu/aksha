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

The research agent and the patent agent work on whatever you give them: a folder or a set of files (saved web pages, Markdown notes, CSV or JSON exports, email text, text extracted from a PDF). The request says what to research, for whom and what to compare; the agents find each input by its content, never by its name or folder. The example below uses the synthetic `tilldew` world (an agritech venture; see [src/aksha/data/README.md](../src/aksha/data/README.md)) as if it were your folder. Run it from a fresh clone, after `make venv`:

```sh
. .venv/bin/activate
export AKSHA_HOME=var        # keep this walk-through's state in ./var (an install uses the per-user data folder; aksha doctor shows it)
cp -r src/aksha/data/worlds/tilldew/sources my-inputs     # stands in for your own folder

aksha run --agent research --input my-inputs "Research the competitors for Tilldew's solar soil-moisture probe, for smallholder vegetable farmers in India. Compare power (solar, battery, AA, cells), battery life (battery life, years of battery) and connectivity (SMS, 2G, LoRa, gateway). What do farmers say about: accuracy (accura*, wrong reading*, false alert*, still wet, trust*); connectivity (sms, signal, network, gateway); price (price*, expensive, cost*, cheap*, paid)?"
# uploaded 23 input files
# session S-...
# R-0001 research ended complete                                          (exit code 0)

aksha show                   # every latest output: research.matrix@v1, ...; stale ones marked
aksha show research.needs    # one output's content as JSON, every value cited
```

Uploaded files become versioned records of your inputs (`var/inputs/<user>/`). Upload the folder again after changing a file, and only the work built on that file reopens:

```sh
aksha upload my-inputs       # nothing changed
# uploaded 23 input files
# no work reopened: no input changed, or none that work was built on
sed -i 's/Too expensive for a two-acre plot./Too expensive, and the battery died./' my-inputs/exports/farmer-reviews.csv
aksha upload my-inputs
# uploaded 23 input files
# R-0002 research ended complete                                          (exit code 0)
aksha inspect R-0002         # its steps: only the review reading (voice) and the report re-ran
```

The patent agent reads the same inputs (invention notes, contributor and disclosure tables, emails, priorities, blank forms, publications) and prepares a counsel-review packet:

```sh
aksha run --agent patent "Prepare Tilldew's invention candidates for counsel review."
#     asks: TD-2: the note leaves Tariff table source undecided. What is it?   (exit code 3)
aksha answer I-0001 "The state utility's published time-of-day tariff table."
#     asks: Which candidate should be prepared for counsel review? TD-1: prioritize_counsel_review; TD-2: collect_more_evidence; TD-3: collect_more_evidence
aksha answer I-0002 TD-1
#     packet: patent.packet/TD-1@v1                                       (exit code 4)
```

The approval and the mock mailbox that receives the packet work as in the stillroom walk-through below. The offline models understand a small grammar in the request: `Compare <dimension> (<terms>), ...` names the matrix's dimensions besides price (a term ending in `*` is a prefix, `not <phrase>` excludes text), `say about: <theme> (<terms>); ...` names the review themes, `for <segment>` and a country or region fix the segment and the geography, and a missing one is asked for. A live model reads the request as free text; see the limitations.

## The command line, step by step

The research agent runs end to end through `aksha run` on the synthetic `stillroom` world, here configured as a folder every run reads (`AKSHA_SOURCES`) instead of uploaded. Run this from a fresh clone, after `make venv`:

```sh
. .venv/bin/activate
export AKSHA_HOME=var        # keep this walk-through's state in ./var (an install uses the per-user data folder; aksha doctor shows it)
rm -rf var                   # an empty store, so the IDs below match
export AKSHA_SOURCES=inputs=src/aksha/data/worlds/stillroom/sources   # a configured inputs folder

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

`AKSHA_SANDBOX` chooses the sandbox backend, which is probed once at start: `bwrap` (bubblewrap, Linux), `e2b` (the E2B cloud, with `E2B_API_KEY` set), `auto` (the strongest local one that probes available, never a cloud one), `refuse` (runs nothing) or `none` (no backend). Each session keeps one sandbox for all its runs (decision 37): with bubblewrap, a folder under `<AKSHA_HOME>/sandbox/bwrap/<user>/<session>/work` bound as `/work`, started from `AKSHA_WORKSPACE` when it is set (a git repository becomes a worktree on the branch `aksha/<session>`, with a checkpoint commit after each exec that changed it; your checkout is never touched). `aksha sandbox list` shows your session workspaces and `aksha sandbox clean <session>` removes one, keeping a git workspace's branch. A backend that does not probe available becomes `refuse` with the reason, never a weaker box, so on a host without bubblewrap the headless run above is refused. `<name>:<strength>` with any other name declares an outer box the operator attests the process runs in, such as a container: a headless run may start, and exec tools are still refused. The choice and every probe are recorded on `run.started`. Tools reach the network only through the runtime's HTTP client, under their egress policy, and only with `AKSHA_NETWORK=on`. With `--agent` left out, the router picks the agent from the loaded agents' purposes. State lives in `var/`; delete it to start again.

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
