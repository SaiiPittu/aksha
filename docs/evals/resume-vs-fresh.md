# Resume against fresh on the scope change (decision 36): fake-mode results

*Status on 26 Nov 2026: the comparison runs end to end in fake model mode, and all three arms rebuild the amended scope correctly. The numbers below come from a fresh clone of the branch that added the arms, with `make venv` and then `aksha eval run change --k 3`. They show that the arms run under their own settings, record their re-entry choices and grade against the derived key. They do not show which mode is better, because the model is a rule-based fake. Fresh stays the default; the maintainer decides after a live-model run.*

## What runs

The eval case is `src/aksha/data/suites/change/change-scope-resume-vs-fresh.toml`, in the `change` eval set. The research agent runs as it ships, on its rule-based fake, against the Stillroom world. The person's request names the segment (offices), the agent asks for the geography, and the scripted user answers "EU". When that request run has finished and its outputs are exported, the change step applies the change set `scope-eu-uk`. It adds one dated line to the priorities note the scope Task reads: "the market research covers the European Union and the United Kingdom". The adapter records the source change in the runtime (`ApplySourceChange`), and the runtime opens one rebuild run and re-enters only the work that read the moved note, directly or through lineage. The same episode runs once per arm:

| Arm | `run.started{reentry}` | Resume policy | Definition hash |
|---|---|---|---|
| `fresh` | `fresh` | the agent's own | the shipped one |
| `resume` | `resume` | the agent's own | the shipped one |
| `resume_scope_exempt` | `resume` | `research.scope` added to `premise_exempt` | a different one, so this arm is never pooled with the other two |

Each arm is graded against the change set's derived key (`src/aksha/data/worlds/stillroom/changes/scope-eu-uk/key.json`). The `scope` and `review_summary` outputs must be saved again after the change (`change.stale_set`). The `competitor` outputs must not be (`change.unchanged_set`, the early-cutoff check). The saved scope must name the regions EU and UK, and the review summary's in-scope theme counts must equal the key's (`research.scope_change`). For those counts, battery goes from 2 to 5 and stillness from 4 to 5 of the 40 unique reviews, and setup (5) and privacy (2) do not move. Citations and the two send invariants are checked as on every episode.

## Results (k = 3 rollouts per arm)

The measures are medians over the arm's rollouts, taken in the rebuild run only. The request run is the same in every arm: 21 model turns and 39 tool calls.

| Arm | Correct against the key | Must criteria passed | Model turns | Tool calls | Input / output tokens | Context characters sent | Re-entries (per rollout) | Why each fresh one was fresh | pass^k |
|---|---|---|---|---|---|---|---|---|---|
| `fresh` | 3 of 3 | 24 of 24 | 11 | 23 | 0 / 0 | 211,638 | 4 fresh | `resume_disabled` (4) | = pass^1, rollouts identical |
| `resume` | 3 of 3 | 24 of 24 | 11 | 25 | 0 / 0 | 215,704 | 1 resumed (scope), 3 fresh | `tainted_session` (discover), `premise_changed` (voice), `session_missing` (report) | = pass^1, rollouts identical |
| `resume_scope_exempt` | 3 of 3 | 24 of 24 | 9 | 23 | 0 / 0 | 183,723 | 2 resumed (scope, voice), 2 fresh | `tainted_session` (discover), `session_missing` (report) | = pass^1, rollouts identical |

Compared with `fresh`, the `resume` arm takes the same model turns, 2 more tool calls and 4,066 more context characters. The `resume_scope_exempt` arm uses 2 fewer model turns, the same tool calls and 27,915 fewer context characters (13% fewer). In all three arms the five `evidence` instances were not re-entered, because the discover Task rebuilt a competitors artifact equal to the old one and the profiles that read it stayed current.

*These rows are from a fresh clone on 26 Nov 2026 after the research agent became general (its request names the review themes; the scope re-reads a note whose earlier read went stale, so a resumed scope now resumes instead of falling back `mostly_stale`). The first measurement, before that change, was: fresh 10 turns, 22 calls, 182,896 characters; resume the same, every re-entry fresh; scope exempt 8, 20, 152,770, the voice Task resumed. The points below describe that first measurement, and items 2 and 3 now hold with the scope resumed as well.*

## What the numbers show

1. The arms run, and each records the setting it was given. Every re-entry in the `fresh` arm records `resume_disabled`. No re-entry in either resume arm does.
2. On this scenario, plain `resume` behaves exactly like `fresh`: every re-entry falls back to fresh. The scope Task's session is mostly the note it read, and that note moved (`mostly_stale`). The discover session holds the competitor page with the planted injection (`tainted_session`). The voice Task renders the scope as a declared read, so its premise moved (`premise_changed`). The report Task's session is a single turn (`session_missing`, under `min_prior_turns = 2`). The fallback triggers decide this, not the model, so this part of the result would hold with a live model, given the same sessions.
3. Only the premise-exempt arm resumes anything, and only the voice Task. The voice Task resumes its own session with the old review read and a change notice, and saves the new in-scope counts in one turn where fresh took three.
4. All three arms are equally correct. With k = 3 and a deterministic fake, the rollouts are identical (k_effective = 1), so pass^k equals pass^1 and says nothing about reliability.

## What the numbers do not show

1. **Which mode is better.** Model turns, tool calls and context characters are properties of the rule-based fake. It decides the next call by fixed rules from what its context holds, so a resumed session that already holds a result skips a call by construction. A live model may read a resumed context differently, ignore the change notice, or repeat work.
2. **Tokens.** The fake reports no usage, so every token count is 0. Context characters are the only size measure here. They are characters sent per model step, summed over the rebuild run, not tokens and not cost.
3. **Reliability.** Identical rollouts make pass^k uninformative. The live run the eval case declares (k = 5) is what can separate the arms.
4. **Generality.** This is one scenario, one world and one agent. The scope change moves only the reviews' geography filter, so the competitor work is untouched in every arm.

## Defects the comparison found and fixed

Running the resume arms on the shipped agent surfaced two defects in resume itself (ADR-P08 B9 and B10, ADR-P13 B4). Each now has a test:

1. A read is recorded once per instance, so the note the scope Task read a second time (after a search had already recorded that version) carried no read on its tool result. When the note moved, that result was never masked as stale, and a resumed scope Task went on from the old text. Tool results now also name the records their cited spans quote.
2. A resumed instance could not cite spans its own session had been shown. Those spans were shown to the session's instance in the earlier run, not to the new run's instance, so every save was refused `span_not_seen` until the stop policy gave up (`evaluation_loop`). The evidence of a resumed instance now counts its session's shown spans; a stale one is still refused.

Before these fixes, both resume arms crashed on this eval case. After them, both pass.

## Reproduce

```text
make venv
.venv/bin/aksha eval run change --k 3 --out var/evals/change
```

The report's arms table is in `var/evals/change/report.txt`, and `report.json` holds the same data under `arms`. Each episode's runtime store is at `var/evals/change/_arms/<arm>/change-scope-resume-vs-fresh/stillroom/r<n>/runtime/aksha.sqlite`.

## Open for the maintainer

1. Whether `research.scope` should be premise-exempt for the research agent. It is the only setting under which resume did anything here, and it changes the definition hash.
2. Whether `min_prior_turns = 2` should count a one-turn session such as the report Task's as resumable.
3. The live run (k = 5, tier 2) that decides the default. Its token counts need a model that reports usage.
