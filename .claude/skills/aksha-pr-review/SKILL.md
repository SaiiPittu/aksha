---
name: aksha-pr-review
description: Review an Aksha pull request with the GitHub CLI against Aksha's own rules (the ADR decision implemented exactly, the dependency rule, store facts and derive interpretations, the single writer and hash chain, idempotency and step semantics, the deny-by-default policy gate, no agent-specific code in the runtime, tests that fail when the guarantee breaks, and the public-repository rules), then submit one structured review with an APPROVE or REQUEST_CHANGES verdict. Use when asked to review, self-review or gate the merge of a pull request in this repository.
allowed-tools: Read, Grep, Glob, Bash, Write
---

# Aksha PR review

This skill reviews one pull request in this repository and submits one review through `gh api`. It is the self-review that decision 35 in [the decisions log](../../../docs/design/04-decisions-log.md) requires before an agent squash-merges: every check green, and a review against the repository's rules. [`REVIEWING.md`](../../../REVIEWING.md) at the repository root is the short version of the same rules for third-party review bots; this skill is the full procedure.

The review answers one question: does this change keep every guarantee Aksha makes, and does it implement the decision its ADR records, exactly? Style, naming taste and refactors nobody asked for are out of scope unless they break a rule below.

Reference files:

| File | Use it for |
|---|---|
| [references/invariants.md](references/invariants.md) | The ten Aksha rules, how to check each one mechanically, and what counts as a blocking finding |
| [references/review-format.md](references/review-format.md) | The `review.json` shape, the body template and the submission and validation commands |

---

## Phase 0: Setup and scope

1. **Pick the PR and read its metadata.**
   ```bash
   REPO=SaiiPittu/aksha
   PR_NUM=<number>
   gh pr view $PR_NUM -R $REPO --json number,title,body,author,baseRefName,headRefName,headRefOid,files,additions,deletions,labels
   gh pr diff $PR_NUM -R $REPO --name-only
   ```
2. **Make a review workspace outside the repository.** Nothing the review writes may land in the tree.
   ```bash
   WORKDIR="/tmp/aksha-review-$PR_NUM"; mkdir -p "$WORKDIR"
   ```
3. **Check out the head in a separate worktree**, so the review never disturbs a working copy another agent is using.
   ```bash
   git fetch origin "pull/$PR_NUM/head:review/$PR_NUM" --quiet
   git worktree add "$WORKDIR/tree" "review/$PR_NUM"
   ```
4. **Classify the risk by path.** Changes under `src/aksha/runtime/storage/`, `src/aksha/runtime/canonical.py`, `src/aksha/runtime/policy/`, `src/aksha/runtime/api.py`, `src/aksha/adapters/storage/` and `tests/architecture/` are high risk: they carry the chain, the gate, the extension API or the guards themselves. Docs-only and fixture-only PRs are lower risk but still get the public-repository gate in full.

---

## Phase 0.4: Intent and ADR alignment

Every code change in Aksha implements a primitive, and every primitive has one ADR in `docs/design/adr/`. Read intent before reading code.

1. Read the PR description, every linked issue (`gh issue view <n> -R $REPO`) and existing review threads (`gh pr view $PR_NUM -R $REPO --comments`). Do not re-raise a point already under discussion; do follow up on one left unresolved.
2. **Name the ADR.** The description must say which primitive (`ADR-Pnn`) the PR implements, and link its issue. A code PR that names no ADR is `REQUEST_CHANGES`: ask which decision it implements rather than guessing.
3. **Read the ADR's Decision section, its amendments and its Interface section**, plus any decisions-log entry it cites. Note each decision's status: Accepted, Accepted in part, Proposed or Open.
4. **Check the code against the decision, exactly.** For each numbered decision the PR touches, the code does what the decision says: the same names, the same order of checks, the same event types and payload fields, the same refusal codes. Three outcomes:
   - Matches: say so in Strengths with the decision number.
   - Diverges, and the PR updates the ADR (an "Amendments from the build" entry) in the same PR, with the reason: acceptable, check the amendment is honest.
   - Diverges silently: `REQUEST_CHANGES`. The ask is "update the ADR to match and say why, or change the code". Silent divergence is worse than an honest amendment, because the next reader trusts the ADR.
5. **Status honesty.** Code may rely on an Accepted decision. Code that implements a Proposed decision says so in its docstring or the PR description. A PR that flips a status in a doc (Proposed to Accepted) without a decisions-log entry is `REQUEST_CHANGES`: only the maintainer accepts a decision.
6. **Docs that must move with the code.** A new module appears in `docs/architecture/codebase-structure.md` (a guard enforces it); a change to `api.py` bumps the extension API version (a snapshot test enforces it); a new event type is in the event catalogue and named in its ADR.

When you cannot resolve intent yourself (the ADR is silent, or a choice could be deliberate or accidental), do not assume the charitable reading and do not drop the concern. Record it under `Questions / Needs Author Input` and the verdict is `REQUEST_CHANGES`.

---

## Phase 0.5: Hygiene gates (mechanical)

These run before any judgement. Their findings go straight into `$WORKDIR/review_provisional.md` under `## Hygiene gate failures`.

### 0.5a: The branch is rebased on main

```bash
BASE=$(gh pr view $PR_NUM -R $REPO --json baseRefName --jq .baseRefName)
HEAD_OID=$(gh pr view $PR_NUM -R $REPO --json headRefOid --jq .headRefOid)
git fetch origin "$BASE" --quiet
BEHIND=$(git rev-list --count "$(git merge-base "origin/$BASE" "$HEAD_OID")..origin/$BASE")
```

`BEHIND > 0` is a hard stop: one review note giving the count, verdict `REQUEST_CHANGES`, skip every later phase. PRs in this repository merge in a fixed order, and a stale branch can silently revert the PR merged before it.

### 0.5b: Checks are green

```bash
gh pr checks $PR_NUM -R $REPO
cd "$WORKDIR/tree" && make venv >/dev/null && make check
```

`make check` runs lint, typecheck and the test suite, including the architecture guards. Any failure is a hard stop with `REQUEST_CHANGES`, quoting the failing check. If the PR touches `src/aksha/adapters/storage/postgres.py` or the storage contract suite, also run `make venv-postgres && make test-postgres`; a PostgreSQL test that skips instead of running does not count as passing.

### 0.5c: Test-suite integrity

```bash
# Test files deleted by the PR
git diff --diff-filter=D --name-only "origin/$BASE..$HEAD_OID" -- 'tests/**'
# Assertion count per changed test file, before and after
for f in $(git diff --name-only "origin/$BASE..$HEAD_OID" -- 'tests/**/*.py'); do
  before=$(git show "origin/$BASE:$f" 2>/dev/null | grep -cE '^\s*assert |pytest\.raises\(' || echo 0)
  after=$(git show "$HEAD_OID:$f" 2>/dev/null | grep -cE '^\s*assert |pytest\.raises\(' || echo 0)
  [ "$after" -lt "$before" ] && echo "DROP $f: $before -> $after"
done
# Newly skipped or expected-to-fail tests
git diff "origin/$BASE..$HEAD_OID" -- 'tests/**' | grep -nE '^\+.*(pytest\.mark\.(skip|skipif|xfail)|pytest\.skip\(|pytest\.importorskip\()'
```

A deleted test file needs a reason in the PR description, or `REQUEST_CHANGES`. An assertion drop is a `COMMENT` for a reader to judge. A new skip or xfail needs a reason and an issue link, or `REQUEST_CHANGES`. A storage contract test that an adapter skips is always `REQUEST_CHANGES`: if an adapter cannot pass the shared suite, the port or the suite is wrong, and that is a design change to raise (architecture section 4.1).

### 0.5d: Public-repository gate

This repository is public. Run the gate on the full diff, including docs, fixtures, comments, commit messages and the PR description.

```bash
git diff "origin/$BASE..$HEAD_OID" > "$WORKDIR/diff.patch"
git log --format='%B' "origin/$BASE..$HEAD_OID" > "$WORKDIR/messages.txt"
gh pr view $PR_NUM -R $REPO --json body --jq .body >> "$WORKDIR/messages.txt"
```

The maintainer keeps a denylist of private names and of external products that must not be named as sources, outside the repository, one term per line. The path is in `AKSHA_REVIEW_DENYLIST`. The list itself is never committed, quoted in a review or echoed into a log: report only which file and line matched.

```bash
if [ -n "$AKSHA_REVIEW_DENYLIST" ] && [ -f "$AKSHA_REVIEW_DENYLIST" ]; then
  grep -niF -f "$AKSHA_REVIEW_DENYLIST" "$WORKDIR/diff.patch" "$WORKDIR/messages.txt" | cut -d: -f1,2
else
  echo "NOTE: no denylist configured; the private-name check ran by reading only"
fi
```

The checks that need no list:

```bash
# Patent-looking numbers in fixtures or docs that are not SYN- prefixed
grep -nE '^\+.*\b(US|EP|WO|JP|CN|KR|DE|GB)[ -]?[0-9]{4,}' "$WORKDIR/diff.patch" | grep -v 'SYN-'
# Absolute home paths, email addresses and anything shaped like a key
grep -nE '^\+.*(/home/[a-z]|/Users/[A-Za-z]|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}|sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16})' "$WORKDIR/diff.patch"
# New fixture files that do not say they are synthetic
for f in $(git diff --diff-filter=A --name-only "origin/$BASE..$HEAD_OID" -- 'src/aksha/data/**'); do
  git show "$HEAD_OID:$f" | grep -qi 'synthetic' || echo "UNLABELLED $f"
done
```

Treatment: any denylist hit, any real-looking patent number, any credential shape or any personal path is `REQUEST_CHANGES`, blocking, and leads the review body. A new fixture source that does not label itself synthetic, either in the file or in its world's `manifest.json`, is `REQUEST_CHANGES`. An email address on an example domain (`example.com`, `example.org`) is not a finding.

Read the prose as well as running the greps. A source list must cite the design's own records, papers, standards or public posts by their authors, never another product's internals as the source of a design. A sentence that only makes sense to someone who knows the maintainer's private context (a client, an employer, a deadline, a hiring step) is a finding even when no listed term matches.

---

## Phase 1: Staged reasoning

Do not write findings while you are still learning the PR.

### 1A: Reconnaissance (no findings)

1. List every changed file and its layer (`runtime/`, `adapters/<family>/`, `extensions/`, `agents/`, `interfaces/`, `app.py`, `tests/`, `docs/`, `data/`).
2. Map which primitive each change belongs to, and which ports, event types, boundaries and dedup keys it adds or changes.
3. Trace every new write path to the event store, every new tool, every new event type and every new external call from its entry to the store.
4. Note which test proves each guarantee the PR claims.

### 1B: Provisional findings

Write each observation to `$WORKDIR/review_provisional.md` with a description, `Confidence: LOW`, `Requires validation:` (the files or modules to read) and `Rule:` (the invariant number from references/invariants.md). These notes are never submitted.

### 1C: Validation and promotion

Re-read every file a provisional finding lists. Drop findings the wider code resolves. Promote the rest LOW to MEDIUM to HIGH to FINAL. Only HIGH or FINAL findings enter `review.json`.

- A finding with a definitive one-hunk fix gets a GitHub `suggestion` block. Anything less certain gets prose only.
- Flag only what this PR introduced or made worse. A pre-existing problem the PR merely touched is at most a `COMMENT`.
- Blocking (`REQUEST_CHANGES`): a broken invariant from references/invariants.md, a silent ADR divergence, a correctness or data-loss bug, a swallowed exception, a guarantee with no test that fails when it breaks, and any public-repository gate failure.
- Non-blocking (`COMMENT`): naming, a clearer error message, a docstring that lags, a test that could be tighter while the guarantee is already covered.
- A concern you cannot settle for lack of context is a question, not a dropped finding (Phase 0.4).

### 1D: Dynamic correction

When a later file contradicts an earlier conclusion, edit or remove that finding in `review.json` at once.

### 1E: Reconciliation

Re-read the whole `review.json`. Every finding still holds, none repeats another, the body tables are a one-line rollup of the inline comments and nothing more, and the verdict follows from the findings.

---

## The rules

The ten rules, with their mechanical checks and examples, are in [references/invariants.md](references/invariants.md). In one line each:

1. **The ADR decision is implemented exactly**, or the ADR is amended in the same PR.
2. **The dependency rule holds.** The runtime imports no adapter, agent, extension, interface or `aksha.app`; agents and extensions import only `aksha.runtime.api`; every `Protocol` lives in a `ports.py`.
3. **Store facts, derive interpretations.** No stored status, counter, flag or remaining budget; every such value is a projection built by a pure reducer.
4. **One writer and an intact hash chain.** Only the `EventStore` adapter writes; no update or delete; the canonical bytes and the chain stay stable.
5. **Idempotency and step semantics.** Every effect goes through the outbox under a key built from meaning; every command carries a command ID; every tool declares `at_least_once`, `reconcile` or `at_most_once` correctly.
6. **Deterministic runtime.** No wall clock, sleep, random or `uuid4` in the runtime; time comes from the `Clock` port and IDs from the `IdSource`.
7. **The policy gate denies by default.** It is pure, fails closed, runs its layers in the fixed order, takes nothing the model can write, and in headless mode only asks become allows while every deny still holds.
8. **No agent-specific code in the runtime.** No agent, fixture world or tool name appears in `src/aksha/runtime/`; a new runtime abstraction is needed by all three agents (the rule of three).
9. **Tests fail when the guarantee breaks.** Each guarantee has a test that breaks if the code is wrong, and a new guard is shown failing on a planted violation before it is trusted.
10. **The public-repository rules hold.** No private names, no external product named as a source, synthetic fixtures labelled synthetic with `SYN-` identifiers.

---

## Phase 2: Verdict and submission

1. **Set the verdict.** `APPROVE` only when the PR is clean through and through: no blocking finding and no open question. Otherwise `REQUEST_CHANGES`. `COMMENT` is a per-finding severity and never the verdict.
2. **Write the body** from the template in [references/review-format.md](references/review-format.md). It always has a Strengths section, citing the decision numbers the PR implements correctly.
3. **Validate** `review.json` with `jq`, check the verdict field, and check every inline comment's `line` is on the right side of a hunk in `gh pr diff`. One bad line fails the whole submission.
4. **Submit.** GitHub refuses `APPROVE` and `REQUEST_CHANGES` on a PR its own author reviews. When the reviewing account is the PR's author (the usual case for a self-review), submit the API event as `COMMENT` and state the verdict on the first line of the body as `**Verdict**: APPROVE` or `**Verdict**: REQUEST_CHANGES`. That first line is the verdict the merge step reads. The exact commands are in references/review-format.md.
5. **Merge only on APPROVE**, and only when the integration order allows it: squash-merge, all checks green, branch 0 commits behind its base. A `REQUEST_CHANGES` verdict sends the PR back to its author, and the fix is re-reviewed from Phase 0.
6. **Clean up.** `git worktree remove "$WORKDIR/tree" --force; rm -rf "$WORKDIR"`.

## Success criteria

A correct review names the ADR decisions the PR implements and checks each against the code, runs every mechanical gate, writes no finding it has not validated, never drops a concern it could not settle, and ends in exactly one verdict that the findings justify.
