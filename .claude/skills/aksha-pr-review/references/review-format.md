# Review format and submission

One review per pass, built as `$WORKDIR/review.json` and submitted with `gh api`. Never write it inside the repository.

## 1. The payload

```json
{
  "event": "REQUEST_CHANGES",
  "body": "**Verdict**: REQUEST_CHANGES\n\n### Aksha PR review\n...",
  "comments": [
    {
      "path": "src/aksha/runtime/policy/gate.py",
      "line": 88,
      "side": "RIGHT",
      "body": "[BLOCKING] Rule 7: this `except` returns `Allow`. ADR-P11 D2 says any exception inside `check` is `Refuse(\"gate_error\")`.\n\n```suggestion\n    except Exception:\n        return Refuse(\"gate_error\", layer=layer)\n```"
    }
  ]
}
```

- `event` starts as `REQUEST_CHANGES` (fail closed) and flips to `APPROVE` only at the end, only with no blocking finding and no open question.
- Each inline comment opens with `[BLOCKING]` or `[COMMENT]` and the rule number, and cites the ADR decision or invariant it rests on.
- A `suggestion` block only when confidence is HIGH and the fix is definitive and fits the hunk.
- Each finding appears once inline, plus at most one row in a body table. The tables are a rollup.
- Build the file with `jq` rather than hand-escaping Markdown into JSON.

## 2. The body template

No emojis. Text markers only: `[BLOCKING]`, `[COMMENT]`, `[PASS]`, `[FAIL]`, `[N/A]`.

```markdown
**Verdict**: APPROVE | REQUEST_CHANGES

### Aksha PR review

**PR**: #<n> <title>
**Implements**: ADR-Pnn decisions <list>; issue #<n>
**Head reviewed**: <short sha>, 0 commits behind main

#### Strengths
Specific things the PR gets right, each with the file or the decision number. For example: "Layer order in `gate.py` matches ADR-P11 D3 exactly, and `test_gate_order.py` asserts the layer number on every refusal."

#### Questions / Needs author input
Only what the review could not settle from the ADRs, the code and `gh`. Omit the section when there are none. Any entry here makes the verdict REQUEST_CHANGES.

#### Gates
| Gate | Result | Note |
| :--- | :--- | :--- |
| Rebased on main | [PASS] | 0 behind |
| Checks (`make check`, CI) | [PASS] | |
| PostgreSQL suite | [N/A] | storage untouched |
| Test integrity | [PASS] | no deletions, no new skips |
| Public-repository gate | [PASS] | |

#### Invariants
| # | Rule | Result | Finding |
| :--- | :--- | :--- | :--- |
| 1 | ADR decision implemented exactly | [PASS] | |
| 2 | Dependency rule | [PASS] | |
| 3 | Store facts, derive interpretations | [PASS] | |
| 4 | One writer, intact hash chain | [N/A] | |
| 5 | Idempotency and step semantics | [FAIL] | `send.py:41`, key includes a timestamp |
| 6 | Deterministic runtime | [PASS] | |
| 7 | Policy gate denies by default | [PASS] | |
| 8 | No agent-specific code in the runtime | [PASS] | |
| 9 | Tests fail when the guarantee breaks | [PASS] | |
| 10 | Public-repository rules | [PASS] | |

#### Change summary
| File | Change |
| :--- | :--- |
| `src/aksha/runtime/...` | One line |

#### Other findings
| Severity | File | Issue | Action |
| :--- | :--- | :--- | :--- |
| [COMMENT] | `...` | ... | ... |

#### Flow
A Mermaid diagram when the PR adds or changes a write path, a recovery path or a gate route. Omit otherwise.

#### Next steps
1. Numbered, one action each, blocking items first.
```

## 3. Validate, then submit

```bash
jq empty "$WORKDIR/review.json" || { echo "review.json is not valid JSON"; exit 1; }
VERDICT=$(jq -r '.event' "$WORKDIR/review.json")
case "$VERDICT" in APPROVE|REQUEST_CHANGES) ;; *) echo "verdict must be APPROVE or REQUEST_CHANGES, got '$VERDICT'"; exit 1 ;; esac
head -1 <<<"$(jq -r '.body' "$WORKDIR/review.json")" | grep -qx "\*\*Verdict\*\*: $VERDICT" \
  || { echo "body must open with the verdict line"; exit 1; }

# Every inline comment must sit on a line inside a diff hunk, or the whole submission fails.
gh pr diff $PR_NUM -R $REPO > "$WORKDIR/pr.diff"
```

GitHub refuses `APPROVE` and `REQUEST_CHANGES` when the reviewer is the PR's author. For a self-review, submit the API event as `COMMENT`; the verdict is the body's first line, and that line is what the merge step reads.

```bash
AUTHOR=$(gh pr view $PR_NUM -R $REPO --json author --jq .author.login)
ME=$(gh api user --jq .login)
if [ "$AUTHOR" = "$ME" ]; then
  jq '.event = "COMMENT"' "$WORKDIR/review.json" > "$WORKDIR/submit.json"
else
  cp "$WORKDIR/review.json" "$WORKDIR/submit.json"
fi
gh api --method POST "/repos/$REPO/pulls/$PR_NUM/reviews" --input "$WORKDIR/submit.json"
```

## 4. After submission

- `APPROVE`: the integration step may squash-merge when every check is green and the branch is still 0 commits behind its base. Re-check both immediately before merging; another PR may have merged in between.
- `REQUEST_CHANGES`: the PR goes back. A fix is reviewed again from Phase 0 of the skill, against the new head.
- Clean up: `git worktree remove "$WORKDIR/tree" --force; rm -rf "$WORKDIR"`.
