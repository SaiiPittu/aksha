# Live models

Offline is the default (`AKSHA_MODE=fake`). Live mode calls every provider through [LiteLLM](https://docs.litellm.ai/docs/providers), so Aksha has one adapter, not one per provider. One general model can fill all three contracts: generation with tool calls, and decide and extract under a forced JSON schema, with every quote still checked word for word in code.

```sh
uv tool install "aksha-harness[live] @ git+https://github.com/SaiiPittu/aksha"
export AKSHA_MODE=live
export GEMINI_API_KEY=...                     # the provider's own variable; LiteLLM reads it, Aksha never records it
export AKSHA_LIVE_MODEL=gemini/gemini-4-neon  # LiteLLM's <provider>/<model> form
```

| Setting | Meaning |
|---|---|
| `AKSHA_LIVE_MODEL` | one model for every alias; `AKSHA_LIVE_MODEL_<CLASS>` (`GENERATION`, `EXTRACTION`, `CLASSIFICATION`) or `AKSHA_LIVE_MODEL_<ALIAS>` overrides it |
| `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ... | the key, in the provider's own variable; a start without it is refused `live_key_missing`, naming the variable |
| `AKSHA_LIVE_TIMEOUT_S` | one attempt's timeout (default 240 seconds for generation, 90 for decide and extract). The name with no suffix sets every class at once, so to change only the generation timeout set `AKSHA_LIVE_TIMEOUT_S_GENERATION`; `_<CLASS>` and `_<ALIAS>` override it |
| `AKSHA_LIVE_REASONING_EFFORT` | `minimal`, `low`, `medium`, `high`, `xhigh` or `none`, passed to the provider's thinking setting |
| `AKSHA_LIVE_BASE_URL` | a local server, such as Ollama, passed as `api_base` |
| `AKSHA_LIVE_SAMPLES` | ask each classification N times and use the votes (default 1) |
| `AKSHA_LIVE_ACTIVE_MS` | a live run's active-time budget (at least an hour by default) |
| `AKSHA_LIVE_TURN_FACTOR` | the multiplier on the run's turn and tool-call budget in a live run, and on any turn limit a Task declares (default 3). Tasks declare none by default (decision 42), so for the research and patent agents it scales the run budget only |
| `AKSHA_LIVE_PROVIDER=chat-completions` | the standard-library adapter instead, for any chat-completions server, with `AKSHA_LIVE_BASE_URL` and `AKSHA_LIVE_API_KEY` |
| `AKSHA_ALIAS_<ALIAS>=module:Class` | a native extraction or classification service for one alias |
| `AKSHA_SEARCH=brave`, `AKSHA_BRAVE_API_KEY`, `AKSHA_NETWORK=on` | live web search, and page fetches under the `aksha.web` egress policy |

A live run records each call's real tokens, LiteLLM's cost estimate when it has a price for the model, and, for a failed attempt, the provider's error type, HTTP status and a short message (never a key). A missing model or install is refused before any call (`live_config_missing`).

## When the provider account has no credits

A provider that says the account has no credits or quota left is not retried. Both live adapters read the provider's error code where it is exposed (OpenAI's `insufficient_quota`, `billing_hard_limit_reached` or `billing_not_active`), else a few exact phrases such as "no credits remaining" or Anthropic's "credit balance is too low". The model step records the one attempt as `step.failed{code: model_quota_exhausted}`, with the provider's error type, status and message, and the run stays open at that step. The CLI prints:

```text
R-0001 cannot move in this process: model provider: no credits remaining (openai); add credits and run aksha resume
```

After adding credits, `aksha resume` asks the model again from the same step and the run goes on. Resuming before the top-up asks once more and prints the same line; it does not count toward the restart limit, so the run is never ended for being resumed too early. A server process (`aksha serve`) skips the run until it restarts.

Every other 429, such as a per-minute rate limit, is still a transient error: it is retried with backoff and waits at least the provider's `Retry-After`. Gemini's per-minute limit says "exceeded your current quota" too, so that phrase counts as no credits only from OpenAI. An invalid key (401 or 403) still ends the step `live_unauthorised`.

What has run live, as of 28 Nov 2026:

- **Smoke tests.** The adapters were tested by hand against `gemini/gemini-4-neon`: one tool call and its follow-up turn, one decide call and one extract call. Each contract held. The API often answered 503 for high demand.
- **Found and fixed: retries.** Live runs ended `blocked` when a 503 spike outlasted two quick retries. A live model step now retries six times with backoff over about two minutes and honours `Retry-After` ([#99](https://github.com/SaiiPittu/aksha/pull/99)).
- **Found and fixed: citations.** A live research run saved its scope, then ended `blocked` at two Tasks' turn limits. Every save of cited content was refused `ungrounded_value` or `quote_too_short`, because the model had never been told how a cite is written. The `save_artifact` description now spells out the cite object, a save reports every grounding error at once, and `AKSHA_LIVE_TURN_FACTOR` scales turn limits for live runs ([#100](https://github.com/SaiiPittu/aksha/pull/100)).
- **A whole task on a real model.** On 28 Nov 2026 the Research Agent ran end to end on `gpt-6.2-sol` (medium reasoning) against the `stillroom` world's offline web: 392 model calls, 3 searches and 8 fetches in 28 minutes. It saved 7 outputs and ended `partial`, with two competitors' evidence left unknown rather than guessed. The recording ships in `src/aksha/data/recordings/research-live-gpt/` and replayed offline to the same outputs (`tests/integration/test_record_replay.py`). It is stale since 28 Nov 2026: each keyed instance's context now names its key, and the `evidence` Task gives a refused finish back to its model, so the recorded answers no longer match what the run sends. Its replay test is marked as an expected failure until it is re-recorded on a live model. The run showed both problems: three `evidence` instances asked which company they were for, one saved another company's profile, and each failed on its first refused finish. The run found three more problems, now fixed: counts the model was asked to supply ([#102](https://github.com/SaiiPittu/aksha/pull/102)), a missing key in checks ([#103](https://github.com/SaiiPittu/aksha/pull/103)) and an unrecorded timeout that broke replay ([#105](https://github.com/SaiiPittu/aksha/pull/105)).
- **Found and fixed: slow listings.** That run spent about 22 of its 28 minutes in `list_inputs`, which decides a kind for each input and each section, 62 decide calls one after another, and asked them all again on every listing and every retry. It also set `AKSHA_LIVE_TIMEOUT_S=240`, which raised the decide timeout from 90 to 240 seconds, so one stalled call cost four minutes and then the whole listing again. Now a run asks each decide or extract request once ([ADR-P07 G8](design/adr/ADR-P07-model-interface-and-routing.md)): a retried listing asks only what its failed attempt had not been answered, and a later listing asks nothing. This too changes what the model is asked, so the bundled `research-live-gpt` recording, already stale, would not replay even against the old contexts; a recording made since says `asks: once_per_run`.
- **Changed: no per-Task turn limit (decision 42).** Live `gpt-6.2-sol` runs failed on per-Task turn limits sized for the offline fakes (4 to 6 turns for the research Tasks, 2 for patent compare and recommend, times 3). Tasks now declare no turn limit by default, as coding agent harnesses run long work: an instance runs until its check passes on a finish, three refused finishes block it, a person stops it, or the run budget holds the run. The budgets are the backstop: live research gets 270 model turns, 420 tool calls and at least an hour of active time, live patent 480 model turns and 2,700 tool calls (ADR-P15 S8).
- **Found and fixed: no credits.** On 28 Nov 2026 a live patent run's first model call was answered with OpenAI's 429 "You have no credits remaining". The adapter read it as a rate limit and retried it seven times over about two minutes, then the run ended `blocked` and could not be resumed. Now no-credit answers are not retried and the run waits at that step for `aksha resume` (the section above).
- **A whole Patent task on a real model.** On 28 Nov 2026 the Patent Agent completed the assignment's example on `gpt-6.2-sol`: it asked the person which candidates to prepare, filled both blank forms and held the packet for approval ([trajectory](../examples/live/patent-2026-11-28/README.md)).
- **Not yet done.** The approve-and-send step has not run live (it is shown offline by `aksha demo`), and the evals have not run in live mode. CI runs live mode only against a stub server on localhost and a stubbed LiteLLM. [Limitations item 11](limitations.md) has the detail.

Retries, backoff, recorded usage and replay of a live run are in the [quick start](quickstart.md) and [ADR-P07](design/adr/ADR-P07-model-interface-and-routing.md).
