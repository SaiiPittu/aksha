# Live Patent run, 28 Nov 2026: complete, C1 prepared for counsel, not sent

A real model run of the Patent Agent on the assignment's own example prompt, exported from its store so a reader can check it without a key. The model was `openai/gpt-6.2-sol` through LiteLLM, and the inputs were the stillroom world's 21 patent files. The first run, `R-0001`, ended `complete` (`completion_checks_passed`) in 16.9 minutes. It asked the person which candidates go to counsel, filled both forms for the one chosen and saved the counsel-review packet. It did not send the packet: the request asked only to prepare it.

Two more runs followed in the same store. `R-0002` was a follow-up sent by mistake to a new session, stopped by hand. `R-0003` was the same follow-up, asking to send the packet, in the right session; it ended `blocked`. Both are described below, because `R-0003` is a real finding about follow-ups.

The code was `main` at `0ba2f2f`, after the fixes in #114 to #122 ([06-evals-and-live-runs.md](../../../docs/assignment/06-evals-and-live-runs.md#patent-before-and-after) has the before and after). On a copy of the store, `aksha verify` printed `chain ok: 1472 events`; events 1 to 1081 are the uploads and `R-0001`.

## What is in this folder

| File | What it is |
| --- | --- |
| `events.jsonl` | all 1,472 events of the store in order, one per line: `owner`, `seq`, `type`, `instance`, `payload` |
| `inspect/R-0001.txt`, `inspect/R-0002.txt`, `inspect/R-0003.txt` | `aksha inspect` of each run: every model and tool step with its event number, and every stop decision |
| `outputs/*.json` | `aksha show` of each saved output, at its latest version |
| `outputs/R-0001/*.json` | the `R-0001` versions of the seven outputs that `R-0002` later saved again, read from the store's `artifacts` table (`aksha show` prints only the latest version) |
| `trace/R-0001/*.otlp.jsonl`, `trace/R-0003/*.otlp.jsonl` | `aksha trace` of each run: the run as OpenTelemetry (OTLP JSON) spans |

The folder is about 2 MB, most of it `events.jsonl`. The store's blob folder (84 KB) is not included: its 21 files are byte for byte the world's own files under `src/aksha/data/worlds/stillroom/sources/`.

## Settings to reproduce

```sh
make venv && . .venv/bin/activate     # with the live extra: pip install -e '.[live,otel]'
export AKSHA_HOME=$PWD/live-run       # a fresh store
export AKSHA_MODE=live
export AKSHA_LIVE_MODEL=openai/gpt-6.2-sol
export AKSHA_LIVE_REASONING_EFFORT=low
export AKSHA_LIVE_TIMEOUT_S=240
export AKSHA_SEARCH=world:stillroom   # the world's offline web
export AKSHA_SYSTEMS=counsel_mail=mailbox:$AKSHA_HOME/mock-counsel   # a development mailbox, never real mail
# OPENAI_API_KEY is read from your environment by LiteLLM; it is never recorded

aksha example stillroom inputs
rm -r inputs/reviews                  # the patent run had the other six folders, 21 files
aksha run --agent patent --input inputs \
  "Which of these inventions should we take to a patent attorney, and why? Prepare the selected disclosure documents."
aksha answer I-0001 C1
aksha resume                          # if the answer does not drive the run to its end
```

The runs were made in the interactive terminal UI (`aksha chat`) with the same settings; the commands above are the command-line equivalent. A model is not deterministic, so a repeat will not match event for event.

## The conversation

1. Prompt (event 24, session `S-30c81cb04260`): "Which of these inventions should we take to a patent attorney, and why? Prepare the selected disclosure documents."
2. After 12.4 minutes of work the agent asked `I-0001` (event 914), a choice with one option per candidate, each labelled with its code-checked next action (`collect_more_evidence` for all four). The question: "Which candidates should go to counsel? All four need more evidence: C1, working firmware, public gate demo, and contribution and assignment questions; C2, sketch, placement prior-art overlap, and unverified walk test and contractor role; C3, customer pilot, with overlap correction, privacy roll-up, and NDA scope to document; C4, public blog disclosure, unmeasured prototype power, and unclear investor-email circumstances." (The model separated each code from its reasons with a dash; the dashes are replaced by commas here. NDA is a non-disclosure agreement.)
3. Answer (event 919, 23 seconds later): C1. The runtime saved `patent.selection@v1` from the answer (event 924, origin `choice`): `{"selected": ["C1"], "recommendations": {"C1": "collect_more_evidence"}}`.

The run asked nothing more and ended at event 1080, 4.1 minutes after the answer.

## What each Task did in R-0001

89 model calls, 186 citations bound. Tool calls: 39 `read_document`, 17 `search_inputs`, 6 `web_search`, 1 `fetch_url`, 6 `list_inputs`, 2 `read_form`, 3 `fill_form` (1 refused), 31 `save_artifact` (6 refused), 1 `ask` and 24 `finish`. Counts are from `inspect/R-0001.txt`.

| Task | Model calls | Tools | Saved |
| --- | --- | --- | --- |
| `scope` | 5 | `list_inputs`, 6 `read_document`, 1 save | `patent.portfolio@v1` (event 72): candidates C1, C4, C3, C2, each with a cited business importance; the plan names both blank forms, `prior_art: true`, `send: false` |
| `candidate[C1]/extract` | 6 | `list_inputs`, 6 `read_document`, 2 saves refused (`quote_not_verbatim`), 1 save, `finish` | `patent.extraction/C1@v1` (event 138) |
| `candidate[C2]/extract` | 4 | `list_inputs`, 4 `read_document`, 1 save, `finish` | `patent.extraction/C2@v1` (event 200) |
| `candidate[C3]/extract` | 4 | `list_inputs`, 5 `read_document`, 1 save, `finish` | `patent.extraction/C3@v1` (event 265) |
| `candidate[C4]/extract` | 4 | `list_inputs`, 4 `read_document`, 1 save, `finish` | `patent.extraction/C4@v1` (event 323) |
| `candidate[C1]/deep/prior_art` | 6 | 6 `search_inputs`, 1 `web_search`, 5 `read_document`, 1 `fetch_url`, 1 save | `patent.prior_art/C1@v1` (event 413): 4 publications in 2 families |
| `candidate[C1]/deep/compare` | 2 | 1 save | `patent.comparison/C1@v1` (event 439): 12 cells |
| `candidate[C1]/deep/recommend` | 5 | 1 save refused (`span_not_seen`), 2 saves | version 1 (event 462) refused by its check, version 2 (event 477) `collect_more_evidence` |
| `candidate[C2]/deep/prior_art` | 5 | 3 `search_inputs`, 1 `web_search`, 2 `read_document`, 1 save | `patent.prior_art/C2@v1` (event 531): 2 publications in 1 family |
| `candidate[C2]/deep/compare` | 2 | 1 save | `patent.comparison/C2@v1` (event 565): 3 cells |
| `candidate[C2]/deep/recommend` | 4 | 2 saves | version 1 (event 583) refused, version 2 (event 597) `collect_more_evidence` |
| `candidate[C3]/deep/prior_art` | 6 | 4 `search_inputs`, 2 `web_search`, 4 `read_document`, 1 save refused (`cite_invalid`), 1 save | `patent.prior_art/C3@v1` (event 669): 4 publications in 2 families |
| `candidate[C3]/deep/compare` | 2 | 1 save | `patent.comparison/C3@v1` (event 701): 10 cells |
| `candidate[C3]/deep/recommend` | 4 | 2 saves | version 1 (event 719) refused, version 2 (event 734) `collect_more_evidence` |
| `candidate[C4]/deep/prior_art` | 7 | 3 `search_inputs`, 2 `web_search`, 1 `read_document`, 2 saves refused (`quote_not_verbatim`, `quote_too_short`), 1 save | `patent.prior_art/C4@v1` (event 797): 1 publication in 1 family |
| `candidate[C4]/deep/compare` | 2 | 1 save | `patent.comparison/C4@v1` (event 829): 4 cells |
| `candidate[C4]/deep/recommend` | 4 | 2 saves | version 1 (event 847) refused, version 2 (event 863) `collect_more_evidence` |
| `table` | 2 | 1 save | `patent.table@v1` (event 892): one row per candidate, each citing its recommendation |
| `select` | 2 | `ask` (`I-0001`), `finish` | `patent.selection@v1` (event 924), saved by the runtime from the answer |
| `prepare[C1]` | 9 | `list_inputs`, 2 `read_form`, `search_inputs`, 2 `read_document`, 2 `fill_form` and 1 refused (`span_not_seen`), `finish` | the two forms (events 988 and 1023) |
| `packet[C1]` | 2 | 1 save, `finish` | `patent.packet/C1@v1` (event 1050) |
| `send[C1]` | 2 | 1 save, `finish` | `patent.handoff/C1@v1` (event 1072) |

Each refused save was a citation the model got wrong: a quote not word for word in its excerpt, a span it had not been shown, or a quote too short to check. Each was fixed on the next save. The recommendation check held for all four candidates. Each first version put free text in `next_action` (for C1, "If you select C1 for counsel review, collect the underlying bench results, ..."). The check refused it with the action the rule table gives, and each second version says `collect_more_evidence`.

## The forms

`prepare[C1]` filled both blank forms from the inputs. A field the inputs cannot settle is marked for counsel or missing, never guessed.

| Form | Fields | Complete | `needs_counsel_confirmation` | `missing` |
| --- | --- | --- | --- | --- |
| `patent.form/C1~invention-disclosure-form@v1` (event 988) | 14 | 10 | 4: `contributors`, `conception_date_and_records`, `outside_resources`, `open_questions_for_counsel` | 0 |
| `patent.form/C1~filing-information-worksheet@v1` (event 1023) | 11 | 2: `working_title`, `attachments` | 6: `applicant`, `proposed_inventors`, `ownership_and_assignments`, `outside_party_agreements`, `earlier_public_disclosures`, `jurisdictions_of_interest` | 3: `related_applications`, `priority_claims`, `representative` |

The packet `patent.packet/C1@v1` opens with the section "Needs counsel confirmation" (inventorship, outside contributions, ownership and three open questions) and carries the scope note "Selected candidate only. Prepared for counsel review, not sent. No legal conclusions."

## The hand-off

The plan had `send: false`, because the request asked to prepare the documents, not to send them. `send[C1]` therefore saved `patent.handoff/C1@v1` with status `not_sent` and the question "Who should receive patent.packet/C1@v1, and do you approve sending this exact version to that recipient?" No approval was asked and nothing was delivered; the mock counsel mailbox stayed empty.

Sending is proven offline only. `aksha demo` shows an approval bound to the exact packet version, and delivery exactly once after the sending process is killed with SIGKILL (a signal a process cannot catch) mid-send ([quickstart](../../../docs/quickstart.md), [04-patent-agent.md](../../../docs/assignment/04-patent-agent.md#offline-with-fake-models)). A live send has not been tried.

## R-0002: a follow-up sent to the wrong session, stopped

The follow-up "Please send the C1 counsel-review packet to our primary patent counsel (counsel-primary) for review." (event 1083) went by mistake to a new session, `S-c61c5cddee89`, and started `R-0002`. It re-planned from scratch: `patent.portfolio@v2` (event 1096) holds only C1, with `forms: []`, `prior_art: false`, `send: true` and the recipient `counsel-primary`. It saved C1 again: a new extraction, a prior art and a comparison both marked `not_checked` (because the new plan turned prior art off), recommendations version 3 and 4, and `patent.table@v2`. It asked the selection again as `I-0002` (event 1319). It was stopped with `aksha stop` (event 1366) before anyone answered; `I-0002` was voided and the run ended `stopped` (event 1368), 5.2 minutes after it started.

## R-0003: the follow-up in the right session, blocked

The same follow-up was then sent to the first session, `S-30c81cb04260` (event 1371), and started `R-0003`. What happened:

1. The run started the declared recipe again from `scope`. Every step up to `table` reused the versions `R-0002` had saved (six `artifact.reused` events, 1375 to 1396), because cross-run reuse is keyed by the agent's writer, not by session ([limitations, item 21](../../../docs/limitations.md)). So this run worked from `R-0002`'s plan: `forms: []` and `send: true`.
2. `select` asked the selection again, `I-0003` (event 1414): "Which candidates should go to counsel? All four are marked collect_more_evidence: ..."
3. While `I-0003` waited, `prepare[C1]` ran, keyed by the selection `R-0001` had saved. Its check `forms_filled` did not count `R-0001`'s two forms: they were not reused (their reads had changed), and a check does not see another run's versions this run did not reuse. With `forms: []` the check asked it to fill each blank form found. The model listed the inputs and read both blank forms (events 1431 to 1454), but filled neither. Three times it tried to finish with text such as "The C1 packet was not sent to `counsel-primary`. The approval-enabled sending tool is unavailable here." The check refused all three, and the third reached the finish-veto limit, which failed the instance (event 1459).
4. The answer C1 came at event 1461, after `prepare[C1]` had already failed. The runtime saved `patent.selection@v2` (event 1466).
5. The run ended `blocked: prepare[C1]` (event 1471), 3.1 minutes after it started. Nothing was sent.

What the follow-up shows: a message in a finished conversation starts the agent's declared recipe again, instead of continuing the conversation from where it ended. The person wanted one step, the send of a packet that already existed; the runtime re-planned, asked the selection again and tried to prepare again. The next step is a single-session design, in which a follow-up continues the conversation it belongs to ([limitations, what comes next](../../../docs/limitations.md#what-comes-next)). Status on 28 Nov 2026: planned, not built.

## How to inspect it

- Read the conversation and every save in `events.jsonl` by `seq`; for example, `interrupt.raised` at 914, `interrupt.resolved` at 919, `artifact.saved` of the packet at 1050, `run.ended` at 1080, 1368 and 1471.
- Each file in `inspect/` lists a run's steps with their event numbers; step IDs such as `R-0001/prepare[C1]/t03` show which instance made each call.
- Load `trace/R-0001/1.otlp.jsonl` into any OTLP viewer, or read the spans as JSON.
- Each `outputs/*.json` holds an output's content with every cited value's quote and span, as `aksha show` printed it. The latest portfolio, C1 extraction, C1 prior art, C1 comparison, C1 recommendation, table and selection are `R-0002`'s and `R-0003`'s; `R-0001`'s versions of those seven are in `outputs/R-0001/`. The packet cites `patent.extraction/C1@v1` and `patent.recommendation/C1@v2`, both in that folder.

No key, token or local path is in these files: they were searched for the key and for the usual key prefix before commit, with no hits. The names in the outputs (inventors, a consultant) are the synthetic world's.
