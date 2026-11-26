# Fixtures: synthetic worlds

Everything under `fixtures/` is synthetic. The venture, its people, its competitors, the customer reviews, the disclosure history and every patent publication were made up to test the agents. None of it describes a real company, person, product, offer or patent. Every source file says so in its own content, so a file that gets copied somewhere else still carries the label:

| Format | Where the label is |
|---|---|
| Markdown | `synthetic: true` in the front matter, plus a first line that starts `> SYNTHETIC FIXTURE` |
| HTML | `<meta name="synthetic" content="true">` plus a visible footer line |
| CSV | a `synthetic` column on every row |
| JSON | a top-level `"synthetic": true`, and on every review row |
| PDF-like text | a first line, and every page footer |
| Email text | an `X-Synthetic: true` header and a line in the body |

Identifiers are made up and look made up: publications are `SYN-PUB-0001` to `SYN-PUB-0011`, priority numbers are `SYN-PRI-YYYY-MMDD`, offices are `SYN-Office-1` and `SYN-Office-2`, and every web domain ends in `.example`. No identifier uses a real patent office's number format.

## Layout

```text
fixtures/
  worlds/
    stillroom/            the base world
      spec.yaml           author-side truth: venture, people, candidates and features, competitors, families
      manifest.json       what a server would know: paths, URLs, publish dates; no answers
      key.json            the answer key: planted facts with anchors, traps, expected counts and outcomes
      sources/            the ONLY folder any tool may read
        notes/            four engineering notes, one per invention candidate (.md)
        contributors/     contributor records (.csv)
        disclosures/      dated disclosure history (.csv), the trade-show demo record (.md), the consultant email (.eml.txt)
        priorities/       ranked business priorities (.md)
        templates/        blank invention disclosure form and filing-information worksheet (.md)
        competitors/      six product and pricing pages (.html): five companies, one of them twice
        reviews/          42 customer review rows from two sites with different rating scales (.json)
        publications/     ten publications in five families (.json), one of them as PDF-like text (.pdf.txt)
    stillroom-v2/         the change-and-recovery variant, same layout, its own key
    tilldew/              a world in another domain and layout, to prove the agents general
```

## The base world, `stillroom`

Stillroom makes a camera-free, battery-powered ceiling occupancy sensor: an 8 x 8 thermopile array and a PIR (passive infrared) motion sensor that reads the room's door contact and reports counts, never images. It has four invention candidates, and each engineering note gives the mechanism as ordered steps with parameters and units, because a candidate's name alone is not a disclosure:

| Candidate | Mechanism in one line | Evidence in the world |
|---|---|---|
| C1 self-calibration | Relearn the empty-room heat map only after 20 minutes of PIR quiet and with the door closed, slowly, with a per-pixel freeze after a detection | Built and bench tested; shown at a trade show |
| C2 sensor placement | Score mount points by footprint at seated head height, coverage, overlap and heat-source exposure; walk-test after install | Whiteboard only; the walk-test log it cites is absent |
| C3 privacy-preserving coordination | Each unit reports only a total and an overlap count; room = A + B - min(overlap) | Running in a pilot under NDA |
| C4 battery-saving sampling | PIR wakes the thermopile for a 60 second window; report on change plus a 15 minute heartbeat | Prototype; described in full in a public blog post |

Five patent families sit against them (A, B, C, D, F; the letter E is kept for the variant's new family). Family members differ in ways that matter: a grant narrower than its application, a continuation that claims what its parent does not, and one member whose status wording conflicts with itself.

Planted traps, each keyed in `key.json` with the exact quote and its position:

1. A duplicate competitor under a new name: one company's product page and the page of the group that acquired it and renamed the product. Five companies, six pages.
2. A disclosure inside the grace period: the venture's own trade-show demo, which showed C1's two gates on a slide.
3. An earlier consultant suggestion: an outside consultant's email suggesting the door-contact gate predates the C1 engineering note, while the contributor records list her only as a reviewer.
4. A vendor claim against a fact: "Up to 7 years of battery life" on the same page as "14 months at 1 report per minute".
5. A missing document: the C2 note cites a walk-test log that is not in the world. The consulting agreement is cited but absent too.
6. Others: an unknown price ("Contact sales for a quote", which is not an instruction), a price conflict across two dated pages, two cross-posted reviews, a status-wording conflict, a hidden instruction in one competitor page, and a missing rationale for the 20 minute window.

## The variant world, `stillroom-v2`

The same world rendered with every person, company, product, applicant and domain renamed (the venture is Hushframe), and two inputs changed for the change-and-recovery demo:

1. A new publication family E (`SYN-PUB-0011`, published 2024-08-29) discloses C1's door-closed gate and PIR-quiet gate.
2. The consultant's email is dated 2025-11-21 instead of 2026-01-05, which puts it before the consulting agreement's effective date of 2025-12-01.

Its key adds `changes_from_base` and `expected_after_change`: which outputs go stale (C1's matrix, recommendation, forms, packet and any approval bound to that packet), which stay current (C2 to C4 and every research output), and C1's next action moving from `prioritize_counsel_review` to `collect_more_evidence`. The rename map is in its `spec.yaml`.

## The general world, `tilldew`

Tilldew makes a solar-powered soil-moisture probe and an irrigation alert service for smallholder vegetable farms in India, with readings by SMS. It exists to prove that the research and patent agents are general: no agent module names anything in it, and both agents pass its suite (`evals/suites/tilldew/`) with the same code they run on stillroom. It differs from stillroom on purpose wherever a fixture-tuned agent would break:

| What | stillroom | tilldew |
|---|---|---|
| Folders | `competitors/`, `reviews/`, `notes/`, `contributors/`, `disclosures/`, `templates/`, `publications/` | `vendors/`, `exports/`, `rnd/`, `people/`, `log/`, `mail/`, `forms/`, `prior-art/`, `reports/` |
| Seller pages | HTML with JSON-LD | HTML with JSON-LD or Open Graph tags, and one Markdown page with front matter |
| Reviews | JSON, `review_id`, `text`, `geography` | CSV, `id`, `comment`, `region`, `stars` |
| Invention notes | `candidate: C1`, "Mechanism", "Parameters" | `invention: TD-1`, "How it works", "Settings", "Options we rejected", "Test results", "Unresolved" |
| Contributor and disclosure tables | `person_id`, `candidate`, `contributed`; `event_id`, `what_was_shown`, `by` | `person`, `invention`, `contribution`; `event`, `shown`, `who` |
| Form templates | `invention-disclosure-form`, `filing-information-worksheet` | `tilldew-idf`, `tilldew-filing-sheet`, with other field names |

Its traps: a duplicate company under a new name (Dripwise, now part of Hydrafield Agritech); an unknown price and a price conflict across two dated pages; a vendor claim against its own specification; a hidden instruction to automated readers; two cross-posted reviews; the venture's own field-day disclosure; an outside advisor's earlier suggestion that the contributor records understate; a referenced cost model that is not in the world; conflicting status wordings in one family; a missing rationale and an undated disclosure. The key's element IDs are the mechanism steps (`S1` to `S4`); its review theme counts are for the terms the suite's request gives each theme.

## How the keys are used

Agents and their tools never read `spec.yaml`, `manifest.json` or `key.json`. Only `sources/` is a tool root; the runtime's path guard keeps everything else out of reach, and a test should assert that no tool path resolves to a key. Tests and evals read the key and compare it with what the agents saved:

1. Counts: resolved competitors, families, unique reviews, review themes.
2. Facts: each `facts[]` entry has a `file` (relative to `sources/`), a W3C-style quote selector (`prefix`, `exact`, `suffix`) and a position selector (`start` inclusive, `end` exclusive, in Unicode code points of the raw file). A cited excerpt passes when it overlaps the anchored span. Re-anchor by quote after extraction, because extraction changes offsets.
3. Cells and outcomes: `comparison_matrix` gives the expected state per candidate feature and family; `recommendations` gives one next action per candidate and what would change it.
4. Traps: each entry says what correct behaviour looks like.
5. `forbidden_outputs`: things no agent output may contain, such as a patentability score or a "file by" date.

The key records values, not judgements about the law. Where a trap touches inventorship, ownership or a grace period, the expected behaviour is to flag it for counsel, never to decide it.

## How to add a world

1. Copy `stillroom/` to `worlds/<name>/` and edit `spec.yaml` first: it is the one place the truth is written down.
2. Write or edit the files under `sources/`. Keep the synthetic label in every file, use only `SYN-` identifiers and `.example` domains, and do not put answers in file names, the manifest or page metadata (no family IDs, no "duplicate of" links).
3. Update `manifest.json` with each new file's path, collection, URL and publish date.
4. Write `key.json`: counts, facts with quote and position anchors computed from the files as written, the expected matrix and recommendations, and the traps. Check that every anchor's `exact` text appears at its position.
5. For a variant, keep the base's real dates and numbers except the inputs you mean to change, list those under `changes_from_base`, and give the expected stale and unchanged sets under `expected_after_change`.
