# Live Research run, 28 Nov 2026: complete, 5 of 5 competitors

A real model run of the Research Agent on the assignment's own example prompt, exported from its store so a reader can check it without a key. The model was `openai/gpt-6.2-sol` through LiteLLM; the web was the synthetic stillroom world's offline web; the only upload was `reviews/reviews.json`. The run ended `complete` (`completion_checks_passed`) in about 8.5 minutes, and `aksha verify` printed `chain ok: 576 events`.

The code was `main` at `b2dacd9`, after the fixes in #114 to #117 ([06-evals-and-live-runs.md](../../../docs/assignment/06-evals-and-live-runs.md#after-the-fixes) has the before and after).

## What is in this folder

| File | What it is |
| --- | --- |
| `events.jsonl` | the run's 576 events in order, one per line: `owner`, `seq`, `type`, `instance`, `payload` |
| `inspect.txt` | `aksha inspect R-0001`: every model and tool step with its event number, and every stop decision |
| `outputs/*.json` | `aksha show` of each saved output: scope, competitors, five profiles, needs, matrix, findings, tests |
| `trace/R-0001/*.otlp.jsonl` | `aksha trace R-0001`: the run as OpenTelemetry (OTLP JSON) spans |

The store's blob folder (the stored copy of the uploaded reviews file) is not included: the file is byte for byte the world's own `src/aksha/data/worlds/stillroom/sources/reviews/reviews.json`.

## Settings to reproduce

```sh
make venv && . .venv/bin/activate     # with the live extra: pip install -e '.[live,otel]'
export AKSHA_HOME=$PWD/live-run       # a fresh store
export AKSHA_MODE=live
export AKSHA_LIVE_MODEL=openai/gpt-6.2-sol
export AKSHA_LIVE_REASONING_EFFORT=low
export AKSHA_LIVE_TIMEOUT_S=240
export AKSHA_SEARCH=world:stillroom   # the world's offline web
# OPENAI_API_KEY is read from your environment by LiteLLM; it is never recorded

aksha example stillroom inputs
aksha run --agent research --input inputs/reviews/reviews.json \
  "Who already solves this problem, what do customers complain about, and what should we test next?"
aksha answer I-0001 "<the answer below>"
aksha resume                          # if the answer does not drive the run to its end
```

The run was made in the interactive terminal UI (`aksha chat`) with the same settings; the commands above are the command-line equivalent. A model is not deterministic, so a repeat will not match event for event.

## The conversation

1. Prompt (event 4): "Who already solves this problem, what do customers complain about, and what should we test next?"
2. The agent read the reviews and asked `I-0001` (event 28): "What problem should the comparison focus on? The supplied reviews mention sensors in meeting rooms, but the request doesn't specify the use case."
3. Answer (event 33): "Camera-free occupancy sensing for meeting rooms in offices, so booking and lighting follow whether a room is really in use. The buyers are facility managers of mid-size office buildings in the EU. Privacy, counting accuracy, installation effort and battery life matter as much as price."

After the answer the run asked nothing more and ended at event 575.

## What each Task did

57 model calls, 3 web searches, 12 page fetches and 147 citations bound, across 9 Task instances. Counts are from `inspect.txt`.

| Task | Model calls | Tools | Saved |
| --- | --- | --- | --- |
| `scope` | 6 | `list_inputs`, `read_document`, `ask` (I-0001), 2 saves | `research.scope` (event 45): question, segment, geography, 4 dimensions, 6 themes |
| `discover` | 7 | 3 `web_search`, 6 `fetch_url`, 2 `read_document`, `search_inputs` | `research.competitors` (event 124): 5 companies on 6 pages, plus one `same_entity_as` relation (event 125) |
| `evidence[corvane-group]` | 7 | 2 `fetch_url`, 4 saves | `research.profile/corvane-group` (event 181): both pages, and a cited price conflict (USD 199 against USD 229 per unit) |
| `evidence[kelvra-systems]` | 7 | 1 `fetch_url`, 4 saves | `research.profile/kelvra-systems` (event 236) |
| `evidence[noctra]` | 7 | 1 `fetch_url`, 4 saves | `research.profile/noctra` (event 289), with 2 flags |
| `evidence[tallyway]` | 7 | 1 `fetch_url`, 4 saves | `research.profile/tallyway` (event 339) |
| `evidence[veyra-labs]` | 7 | 1 `fetch_url`, 4 saves | `research.profile/veyra-labs` (event 389) |
| `voice` | 5 | `list_inputs`, 2 `read_document`, 1 save | `research.needs` (event 432): 42 rows, 2 cross-posted duplicates, 40 unique; 6 themes, each cited |
| `report` | 4 | 3 saves | `research.matrix` (494), `research.findings` (528, 8 findings, 4 unknowns marked `not_checked`), `research.tests` (556, 5 tests, thresholds marked `proposal`) |

Each keyed `evidence` instance saved its profile under its own key, the failure that #114 fixed.

## Against the world's answer key

The key is `src/aksha/data/worlds/stillroom/key.json`; no agent tool can read it.

| Key | Answer key | This run |
| --- | --- | --- |
| K1 | Kelvra Systems | `kelvra-systems` |
| K2 | Veyra Labs | `veyra-labs` |
| K3 | Tallyway | `tallyway` |
| K4 | Noctra | `noctra` |
| K5 | Brightfield Analytics, acquired by Corvane Group (aliases OccuPoint, Corvane Presence P1), on two pages | `corvane-group`, both pages, merged by a `same_entity_as` relation |

All 5 match. The K5 merge (event 125) cites the Corvane page: "Corvane Presence P1 is the new name for OccuPoint. Brightfield Analytics joined Corvane Group on 12 May 2026, and the product line continues under the Corvane brand." The needs count battery life in 7 of 40 reviews, the key's count.

## How to inspect it

- Read the conversation and every save in `events.jsonl` by `seq`; for example, `interrupt.raised` at 28, `relation.saved` at 125, `run.ended` at 575.
- `inspect.txt` lists each step with its event number; step IDs such as `R-0001/evidence[noctra]/t02` show which instance made each call.
- Load `trace/R-0001/2.otlp.jsonl` into any OTLP viewer, or read the spans as JSON.
- Each `outputs/*.json` holds an output's content with every cited value's quote and span, as `aksha show` printed it.

No key, token or local path is in these files: they were searched for the key and for the usual key prefix before commit, with no hits.
