# Bundled data: synthetic worlds and eval suites

Everything under `src/aksha/data/` is synthetic. It ships inside the package, so `aksha demo` and `aksha eval` find it after `pip install` with no clone; `aksha.data` is the one place code finds a world or a suite. The venture, its people, its competitors, the customer reviews, the disclosure history and every patent publication were made up to test the agents. None of it describes a real company, person, product, offer or patent. Every source file says so in its own content, so a file that gets copied somewhere else still carries the label:

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
src/aksha/data/
  worlds/
    stillroom/            the base world
      spec.yaml           author-side truth: venture, people, candidates and features, competitors, families
      manifest.json       what a server would know: paths, URLs, publish dates; no answers
      key.json            the answer key: planted facts with anchors, traps, expected counts and outcomes
      web.json            the world's web: each page's URL, its date, and which pages are noise (graders only)
      web/                the pages the world's offline web serves, each at web/<host><path>: the six vendor
                          product pages (five companies, one of them twice) and the noise around them: a
                          listing site's copy, the vendor's archived 2025 page, a deal site, a hotel report,
                          a glossary, a publication mirror and a filing-service advert (.html, .json)
      sources/            what a person uploads: the only folder the input tools read
        notes/            four engineering notes, one per invention candidate (.md)
        contributors/     contributor records (.csv)
        disclosures/      dated disclosure history (.csv), the trade-show demo record (.md), the consultant email (.eml.txt)
        priorities/       ranked business priorities (.md)
        templates/        blank invention disclosure form and filing-information worksheet (.md)
        reviews/          42 customer review rows from two sites with different rating scales (.json)
        publications/     ten publications in five families (.json), one of them as PDF-like text (.pdf.txt)
    stillroom-v2/         the change-and-recovery variant, same layout, its own key and web
  recordings/
    research-occupancy/   one recorded fake run (aksha replay runs it again offline; see its README)
    tilldew/              a world in another domain and layout, to prove the agents general
    cindral/              a third domain whose inputs also have other shapes, to prove them structure-agnostic
    vantlow/              a held-out research-only world (log management), written blind to the agent code
    pulvane/              a held-out medical-device world for the patent agent, authored without reading the agent
    quillmere/            a held-out patent world, written without reading the agent code
    coldwick/             a portfolio of twenty invention candidates, for the supervisor Task
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

Tilldew makes a solar-powered soil-moisture probe and an irrigation alert service for smallholder vegetable farms in India, with readings by SMS. It exists to prove that the research and patent agents are general: no agent module names anything in it, and both agents pass its suite (`src/aksha/data/suites/tilldew/`) with the same code they run on stillroom. It differs from stillroom on purpose wherever a fixture-tuned agent would break:

| What | stillroom | tilldew |
|---|---|---|
| Folders | `reviews/`, `notes/`, `contributors/`, `disclosures/`, `templates/`, `publications/` | `vendors/`, `exports/`, `rnd/`, `people/`, `log/`, `mail/`, `forms/`, `reports/` |
| On the web | the vendor pages | the vendor pages and the five prior-art publications (`patents.synthetic.example`), with noise: a listing copy, a sale site, a glossary, a skin-moisture probe review, an aquarium probe publication, a publication mirror, a filing advert |
| Seller pages | HTML with JSON-LD | HTML with JSON-LD or Open Graph tags, and one Markdown page with front matter |
| Reviews | JSON, `review_id`, `text`, `geography` | CSV, `id`, `comment`, `region`, `stars` |
| Invention notes | `candidate: C1`, "Mechanism", "Parameters" | `invention: TD-1`, "How it works", "Settings", "Options we rejected", "Test results", "Unresolved" |
| Contributor and disclosure tables | `person_id`, `candidate`, `contributed`; `event_id`, `what_was_shown`, `by` | `person`, `invention`, `contribution`; `event`, `shown`, `who` |
| Form templates | `invention-disclosure-form`, `filing-information-worksheet` | `tilldew-idf`, `tilldew-filing-sheet`, with other field names |

Its traps: a duplicate company under a new name (Dripwise, now part of Hydrafield Agritech); an unknown price and a price conflict across two dated pages; a vendor claim against its own specification; a hidden instruction to automated readers; two cross-posted reviews; the venture's own field-day disclosure; an outside advisor's earlier suggestion that the contributor records understate; a referenced cost model that is not in the world; conflicting status wordings in one family; a missing rationale and an undated disclosure. The key's element IDs are the mechanism steps (`S1` to `S4`); its review theme counts are for the terms the suite's request gives each theme.

## The world of other shapes, `cindral`

Tilldew proves the agents do not depend on stillroom's names, but it keeps stillroom's structure: the same traps in the same kinds of file, the same table shapes, the same two forms. Cindral, booking and waitlist software for independent physiotherapy clinics in the UK, changes the structure itself, so an agent that recognised inputs by their layout fails it. Every record is decided by kind and extracted by that kind's schema (ADR-P07 G3), through the same code as the other worlds (`src/aksha/data/suites/cindral/`, 11 cases).

| What | stillroom and tilldew | cindral |
|---|---|---|
| Competitors | one HTML or Markdown page per product | text extracted from two PDFs (a brochure, a datasheet: no markup, hard-wrapped lines, a price inside a sentence), a tab-separated price comparison pasted from a spreadsheet (plan, price, provider; one seller found nowhere else), an HTML page with reversed attribute order, self-closing meta tags, a JSON-LD `@graph` and prices split across `<span>`s, and a page with no price |
| The renamed company | two dated HTML pages | a PDF datasheet ("is now part of") and an HTML page |
| Price conflict | two dated pages of one seller | a brochure and the pasted spreadsheet |
| Reviews | a flat JSON list or CSV | a JSON export with nested fields: `meta.locale.country` spelled out, `body.comment`, `score.value` of `score.out_of` |
| Invention candidates | Markdown notes with a mechanism heading and numbered steps | meeting notes in plain text (colon headings, bullet steps, a US-style date, an `Idea ref:` line) and a slide-deck export (`Slide N:` titles, the ID leading a line) |
| Contributors | a CSV with a person ID column | nested JSON with no person ID |
| Disclosures | a CSV log and a linked write-up | a Markdown table inside a note, columns in another order, one empty date |
| Priorities | a numbered Markdown list | a CSV with a rank column |
| Forms | two Markdown templates (`Field`, `Required`) | one firm's own intake form (`Field name`, `Mandatory`, counsel-only fields) |
| Publications | JSON with one layout, WIPO ST.9 text | labelled text and a JSON export with other keys (`doc_number`, `biblio.dates`, `legal.status`) |

Its traps: a company under a new owner across PDF text and HTML; an unknown price and a conflict between a brochure and a spreadsheet; a vendor claim against its own pilot figure; a hidden instruction to automated readers; two cross-posted reviews; the venture's own public demo of one invention; a document named on a slide that is not in the world; two status wordings in one publication; a missing rationale and an undated disclosure. It has no outside-contributor trap, and other counts: two candidates, four companies (one known only from the spreadsheet), three publications in two families, 18 review rows.

## The held-out world, `pulvane`

Pulvane makes a finger-worn ring whose optical pulse sensor and accelerometer flag pulse patterns suggestive of an irregular heart rhythm. It is a patent-side world only (no competitor pages, no reviews), written by an author who read the world and suite formats but not the agent code or its offline models, so its suite (`src/aksha/data/suites/pulvane/`, 7 cases) tests that nothing specific to the earlier worlds leaked into the agent or the harness. Its inputs take shapes no other world uses: a WebVTT meeting transcript with voice tags and a spoken recap of the settings, an automatic call transcript with a US-style date, a plain-text slide export whose speaker notes carry facts the slides do not, a YAML contributor export, a key-value sharing register with one empty date, the venture's own device summary as a regulator published it, OKRs in Markdown, a numbered attorney questionnaire, and five publications in three families inside one search-export JSON. Three candidates (`ENG-104`, `ENG-117`, `ENG-123`), one recommended for each next action.

Its traps: an outside clinician who proposes an element out loud in the design meeting, with no agreement and a hospital IP policy, whom the contributor export credits with no design input; the venture's own disclosure arriving through a regulator's public database; a bench data file named only in speaker notes; a granted member that is In force on its front page and Lapsed in the register; a missing rationale raised as an open point in the transcript; an undated register block; and a current cap that differs between a slide and its speaker notes (no deterministic check). Its suite's grader self-tests pass since the graders read each world's key (ADR-P22 G7), so `aksha eval run pulvane` needs no flag.
## The held-out patent world, `quillmere`
Quillmere Soil Systems makes a capacitance soil-moisture probe whose firmware finds each site's field capacity on its own, from the plateau the soil reaches as it drains after a wetting. The world was written blind: its author read only the world format, the key and suite formats and the loader, never the agent code or its offline models, so nothing in it is tailored to what the agents already handle. It is patent-only (`src/aksha/data/suites/quillmere/`, 7 cases) and in-distribution for that task family: review candidates against prior art, recommend a next action, prepare the strongest for counsel.
| What | quillmere |
|---|---|
| Candidates | five: two in a bound lab notebook transcribed to plain text (dated page headers, lettered steps, a dated margin note, an empty witness line), three in an idea-tracker XML export (`<step>` and `<comment>` elements) |
| Contributors | a YAML roster with nested `worked_on` lists and no person IDs |
| Agreements | a Word export of a university research agreement with numbered clauses |
| Disclosures | JSON Lines with a header row, plus a conference poster as PDF text |
| Priorities | lines of a board update in plain text |
| Form | counsel's HTML questionnaire (`label` and `input` pairs, a disabled counsel-only fieldset) |
| Publications | four in three families: two XML documents, a PDF-text grant with INID codes, a Markdown file with front matter |
Its traps: two candidates whose steps are each found, but split across two families (one is business priority 2 and its author calls it the strongest; the other the venture already described in public release notes); a university adviser labelled "not an inventor" who suggested the key step, under a joint-ownership clause and a filing-notice duty, beside a contractor with no IP clause; the university group's public poster showing the key step four months before the notebook's first entry; a step added later in the notebook margin, in an unwitnessed entry; and the same threshold written in other units in the poster. Two trap IDs reuse a registered check (`T-CONSULT`, `T-GRACE`) for a new kind of situation; `T-OBVIOUS-4`, `T-OBVIOUS-5`, `T-NOTEBOOK` and `T-UNITS` have no trap check yet, so their cases grade through the matrix, next-action, extraction and citation criteria.

## The portfolio world, `coldwick`

Coldwick makes cold-chain temperature loggers and reusable shippers for pharmacy and chilled-food logistics. It holds twenty invention candidates (`CW-01` to `CW-20`), one design note each in tilldew's layout, so the patent agent runs them under its supervisor Task (more than eight candidates; ADR-P15 S5) and the `portfolio` suite (`src/aksha/data/suites/portfolio/`) measures the supervisor. Its publications are `SYN-PUB-0201` to `SYN-PUB-0220`: two restate a candidate's every step, the rest share only a title word with one.

The key's `portfolio` section holds each candidate's profile (how far it is built, its priority rank, any public disclosure, a missing document, whether a publication shows every step), the triage score and next action they imply, which candidates the deep work would keep (`should_admit`) and which it would not (`should_drop`), and one planted wrong drop: `CW-15` is built, a top-eight priority and shows an element no publication has, so the rule table puts it first, but a public trade-fair photo costs it two points of triage score, so a cut on the score drops it. It sits on a key the seeded audit samples, on purpose: a sample cannot catch a miss outside itself, which is why the miss rate is reported.

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

## The held-out research world, `vantlow`

Vantlow, a developer-tools startup, is shortlisting a hosted log-management product. The world was written from the world format alone, without reading the research agent's code or its offline models, so a failure here points at something the agent learned from the other worlds' tasks rather than at the world. Prices are metered (per GB ingested with included quotas, per TB ingested, per million log lines), a vendor's own query benchmark sits beside an independent one, and the reviews are forum posts in three saved HTML threads. Its traps are of other kinds: a rebrand rather than an acquisition, a price conflict across that rebrand, a vendor benchmark against an independent benchmark in another file, a hidden aside between forum posts, cross-posts across threads, a billing unit that does not convert to bytes, a free quota with no paid price, and a reply that quotes another post (`src/aksha/data/suites/vantlow/`, 9 cases). Its author-side truth is `worlds/vantlow/spec.yaml`.
