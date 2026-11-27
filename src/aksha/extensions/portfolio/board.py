"""The work board and everything code derives from it: pure functions over a Task view.

The board is a projection: one row per item, computed from the log every time it is read,
never stored. A row joins what the runtime already records:

==================  ==========================================================  ===========
Column              From                                                        Written by
==================  ==========================================================  ===========
``key``, ``title``  the item listing; the card                                  code
``triage``,         each tier's instance status (``TaskView.child_status``)     derived
``deep``
``state``           the item's latest directive (``auto`` when unsupervised)    supervisor
``tier``            ``deep`` when the item gets deep work, else ``triage``      derived
card columns        ``PortfolioSpec.card``, from the item's typed artifacts     code
``basis_changed``   the triage output's version now differs from the one the    derived
                    directive recorded
``audit``,          whether code sampled the dropped item for deep work, and    code
``worth``           what the deep tier then said about it
``turns``,          model turns and tool calls spent on the item so far         derived
``calls``           (``TaskView.spent``)
``reason``          the directive's reason (120 characters at most)             supervisor
==================  ==========================================================  ===========

What the supervisor reads is :func:`render` (through the ``Recite`` stop policy): counts,
the bounds, the audit, the goal's aggregated failing check and at most ``page_rows`` rows.
It never reads a worker's transcript, a document or a whole artifact.

The audit (super-agent design section 6.2): code, not the supervisor, gives deep work to a
sample of the dropped items, chosen by a seeded hash so the same drops give the same
sample. An audited item the deep tier says was worth keeping is a miss; the miss rate is
misses over audited items with a deep result. While a miss is still dropped and the rate
is above ``max_miss_rate``, the goal fails with "re-open drops", for the supervisor.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aksha.runtime.api import CheckResult

from .kinds import DIRECTIVE, ROUND
from .spec import MAX_COLUMNS, TITLE_CHARS, PortfolioSpec

FIRST = 5
"""How many keys a failing check names; the rest are counted."""


def _field(content: Mapping[str, Any], path: str) -> Any:
    value: Any = content
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def items(view: Any, spec: PortfolioSpec) -> tuple[str, ...] | None:
    """The item keys, sorted as strings; None before the listing exists."""
    kind, path = spec.items
    found = view.latest(kind)
    if found is None:
        return None
    listed = _field(found.content, path)
    if not isinstance(listed, Sequence) or isinstance(listed, str):
        return ()
    keys = {str(i.get("key", i.get("id")) if isinstance(i, Mapping) else i) for i in listed}
    return tuple(sorted(keys))


def supervised(view: Any, spec: PortfolioSpec) -> bool:
    """True when the items are more than ``supervise_above``: the supervisor decides."""
    keys = items(view, spec)
    return keys is not None and len(keys) > spec.supervise_above


def directives(view: Any) -> dict[str, Mapping[str, Any]]:
    """Each item's latest directive content, by key."""
    return {str(k): v.content for k, v in view.all_latest(DIRECTIVE).items() if k is not None}


def last_round(view: Any) -> Mapping[str, Any] | None:
    """The latest round the supervisor committed, or None before the first."""
    found = view.latest(ROUND)
    return None if found is None else found.content


def with_state(found: Mapping[str, Mapping[str, Any]], state: str) -> tuple[str, ...]:
    """The keys whose directive is in ``state``, sorted."""
    return tuple(sorted(k for k, d in found.items() if d.get("state") == state))


def audit_order(keys: Iterable[str], seed: int) -> list[str]:
    """``keys`` in the audit's seeded order: a hash of the seed and the key, then the key."""

    def rank(key: str) -> tuple[str, str]:
        return hashlib.sha256(f"{seed}:{key}".encode()).hexdigest(), key

    return sorted(keys, key=rank)


def audit_sample(dropped: Iterable[str], before: Iterable[str], spec: PortfolioSpec) -> list[str]:
    """The audit after a commit: what was audited before, plus the first dropped by the order.

    Monotone: an item audited once stays audited, so its deep result keeps counting. The
    sample tops up to ``audit_sample`` items from the drops not yet audited.
    """
    kept = sorted(set(before))
    fresh = [k for k in audit_order(set(dropped) - set(kept), spec.audit_seed)]
    room = max(0, spec.audit_sample - len(kept))
    return sorted({*kept, *fresh[:room]})


def audited(view: Any) -> tuple[str, ...]:
    """The keys code sampled for the audit, as the latest round recorded them."""
    found = last_round(view)
    return tuple(found.get("audit", ())) if found else ()


def deep_keys(view: Any, spec: PortfolioSpec) -> frozenset[str]:
    """The items the deep tier works on: every item unsupervised, else admitted plus audited."""
    keys = items(view, spec) or ()
    if not supervised(view, spec):
        return frozenset(keys)
    found = directives(view)
    return frozenset(with_state(found, "admitted")) | frozenset(audited(view))


def not_deep(spec: PortfolioSpec) -> Any:
    """The deep tier's key filter: a key passes (is left out) unless it gets deep work."""

    def not_admitted_to_deep(view: Any) -> CheckResult:
        return CheckResult(view.key not in deep_keys(view, spec))

    return not_admitted_to_deep


@dataclass(frozen=True)
class Audit:
    """The audit as code reads it: who was sampled, what the deep tier said, the miss rate."""

    sampled: tuple[str, ...]
    results: Mapping[str, bool]
    misses: tuple[str, ...]
    open_misses: tuple[str, ...]

    @property
    def miss_rate(self) -> float | None:
        """Misses over audited items with a deep result; None before any result."""
        return len(self.misses) / len(self.results) if self.results else None


def audit(view: Any, spec: PortfolioSpec) -> Audit:
    """The audit of the latest round (module doc)."""
    sampled = audited(view)
    found = directives(view)
    results: dict[str, bool] = {}
    for key in sampled:
        worth = spec.worth(view, key)
        if worth is not None:
            results[key] = bool(worth)
    misses = tuple(k for k in sampled if results.get(k))
    still = tuple(k for k in misses if found.get(k, {}).get("state") == "dropped")
    return Audit(sampled, results, misses, still)


def basis_of(view: Any, spec: PortfolioSpec, key: str) -> str | None:
    """The current version of the item's triage output, as a ref, or None before it."""
    found = view.latest(spec.basis, key)
    return None if found is None else str(found.ref)


def _named(keys: Sequence[str]) -> str:
    shown = ", ".join(keys[:FIRST])
    more = len(keys) - FIRST
    return shown + (f" and {more} more" if more > 0 else "")


def goal(spec: PortfolioSpec) -> Any:
    """The supervisor's completion check: every item carded, then decided, the audit clean.

    Each failing branch names who can clear it (``clears``) and aggregates its keys: one
    message with a count and the first five keys, never one line per item.
    """

    def portfolio_goal(view: Any) -> CheckResult:
        keys = items(view, spec)
        if keys is None:
            return CheckResult(False, "no item listing yet", clears="worker")
        uncarded = [k for k in keys if view.latest(spec.basis, k) is None]
        if uncarded:
            return CheckResult(False, f"{len(uncarded)} items have no triage result: "
                               f"{_named(uncarded)}", len(uncarded), tuple(uncarded[:FIRST]),
                               clears="worker")  # fmt: skip
        if not supervised(view, spec):
            return CheckResult(True)
        found = directives(view)
        undecided = [k for k in keys if k not in found]
        if undecided:
            message = (
                f"{len(undecided)} items have no decision: admit each to the "
                f"{spec.deep.id} tier or drop it with a reason ({_named(undecided)})"
            )
            return CheckResult(False, message, len(undecided), tuple(undecided[:FIRST]),
                               clears="supervisor")  # fmt: skip
        moved = [k for k in keys if found[k].get("basis") != basis_of(view, spec, k)]
        if moved:
            message = (
                f"{len(moved)} decisions rest on a triage result that changed: decide "
                f"them again ({_named(moved)})"
            )
            return CheckResult(False, message, len(moved), tuple(moved[:FIRST]),
                               clears="supervisor")  # fmt: skip
        found_audit = audit(view, spec)
        rate = found_audit.miss_rate
        if found_audit.open_misses and rate is not None and rate > spec.max_miss_rate:
            misses = list(found_audit.open_misses)
            message = (
                f"re-open drops: the audit found {len(found_audit.misses)} of "
                f"{len(found_audit.results)} sampled drops worth {spec.deep.id} work "
                f"(miss rate {rate_text(len(found_audit.misses), len(found_audit.results))}); "
                f"still dropped: {_named(misses)}"
            )
            return CheckResult(False, message, len(misses), tuple(misses[:FIRST]),
                               clears="supervisor")  # fmt: skip
        return CheckResult(True)

    return portfolio_goal


def _short(value: Any) -> Any:
    if isinstance(value, str):
        return value[:TITLE_CHARS]
    return value


def rows(view: Any, spec: PortfolioSpec) -> list[dict[str, Any]]:
    """Every board row (module doc), sorted by score, highest first, then by key."""
    keys = items(view, spec) or ()
    found = directives(view)
    triage = view.child_status(spec.triage)
    deep = view.child_status(spec.deep.task)
    sampled = set(audited(view))
    deep_set = deep_keys(view, spec)
    on = supervised(view, spec)
    out: list[dict[str, Any]] = []
    for key in keys:
        d = found.get(key)
        card = dict(list(spec.card(view, key).items())[:MAX_COLUMNS])
        title = card.pop(spec.title, None) if spec.title else None
        spent = view.spent(key)
        row: dict[str, Any] = {
            "key": key,
            "title": _short(title),
            "triage": triage.get(key, "new"),
            "deep": deep.get(key, "-") if key in deep_set else "-",
            "state": (d.get("state") if d else "undecided") if on else "auto",
            "tier": spec.deep.id if key in deep_set else "triage",
            **{k: _short(v) for k, v in card.items()},
            "basis_changed": d is not None and d.get("basis") != basis_of(view, spec, key),
            "audit": key in sampled,
            "worth": spec.worth(view, key) if key in deep_set else None,
            "turns": spent["model_turns"],
            "calls": spent["tool_calls"],
            "reason": (d.get("reason") or "") if d else "",
        }
        out.append(row)

    def order(row: Mapping[str, Any]) -> tuple[float, str]:
        score = row.get(spec.score)
        number = float(score) if isinstance(score, int | float) else float("-inf")
        return -number, str(row["key"])

    return sorted(out, key=order)


def bounds(view: Any, spec: PortfolioSpec) -> dict[str, int]:
    """The caps code enforces at commit, and what the current decisions commit of them."""
    found = directives(view)
    admitted = len(with_state(found, "admitted"))
    sampled = len(set(audited(view)) - set(with_state(found, "admitted")))
    return {
        "deep_cap": spec.deep.cap,
        "deep_effort": spec.deep.effort,
        "deep_budget_turns": spec.budget_turns,
        "committed_turns": (admitted + sampled) * spec.deep.effort,
        "audit_sample": spec.audit_sample,
        "batch_cap": spec.batch_cap,
    }


def render(spec: PortfolioSpec) -> Any:
    """The supervisor's recitation: the board as data (module doc). Pure over the view."""
    check = goal(spec)

    def work_board(view: Any) -> tuple[str, ...]:
        board = rows(view, spec)
        found = directives(view)
        counts = {
            "items": len(board),
            "triaged": sum(1 for r in board if r["triage"] == "done"),
            "admitted": len(with_state(found, "admitted")),
            "dropped": len(with_state(found, "dropped")),
            "deferred": len(with_state(found, "deferred")),
            "undecided": sum(1 for r in board if r["state"] == "undecided"),
            "basis_changed": sum(1 for r in board if r["basis_changed"]),
            "deep_done": sum(1 for r in board if r["deep"] == "done"),
        }
        a = audit(view, spec)
        audit_line = {
            "sampled": list(a.sampled),
            "seed": spec.audit_seed,
            "deep_results": len(a.results),
            "misses": list(a.misses),
            "open_misses": list(a.open_misses),
            "miss_rate": rate_text(len(a.misses), len(a.results)),
        }
        result = check(view)
        lines = [
            "WORK BOARD " + json.dumps(counts, sort_keys=True),
            "BOUNDS " + json.dumps(bounds(view, spec), sort_keys=True),
            "AUDIT " + json.dumps(audit_line, sort_keys=True),
            "GOAL " + ("met" if result.ok else f"[{result.clears}] {result.message}"),
            f"ROWS by {spec.score}, highest first, at most {spec.page_rows}:",
        ]
        lines += [json.dumps(r, sort_keys=True) for r in board[: spec.page_rows]]
        if len(board) > spec.page_rows:
            lines.append(f"({len(board) - spec.page_rows} more rows not shown)")
        return tuple(lines)

    return work_board


def review_of(
    found: Mapping[str, Mapping[str, Any]],
    round_: Mapping[str, Any] | None,
    scores: Mapping[str, Any],
    worth: Mapping[str, bool | None],
    *,
    supervised: bool = True,
) -> dict[str, Any]:
    """The batched review from plain contents: decisions, the drop list, the audit.

    ``found`` is each item's latest directive content, ``round_`` the latest round,
    ``scores`` each item's board score and ``worth`` the deep tier's verdict where it ran.
    One function, so the review a Task saves and the one a check expects never differ.
    """
    sampled = tuple(round_.get("audit", ())) if round_ else ()
    results = {k: bool(worth[k]) for k in sampled if worth.get(k) is not None}
    misses = [k for k in sampled if results.get(k)]
    dropped = []
    for key in with_state(found, "dropped"):
        dropped.append({
            "key": key, "score": scores.get(key), "reason": found[key].get("reason"),
            "audited": key in sampled, "worth": results.get(key),
        })  # fmt: skip
    dropped.sort(key=lambda r: (-(r["score"] if isinstance(r["score"], int | float) else 0),
                                r["key"]))  # fmt: skip
    return {
        "supervised": supervised,
        "admitted": list(with_state(found, "admitted")),
        "deferred": list(with_state(found, "deferred")),
        "dropped": dropped,
        "audit": {
            "sampled": list(sampled),
            "misses": misses,
            "miss_rate": rate_text(len(misses), len(results)),
            "miss_rate_ppm": rate_ppm(len(misses), len(results)),
        },
    }


def rate_text(misses: int, results: int) -> str | None:
    """A miss rate as the review shows it: ``"1 of 3"``; None before any deep result."""
    return f"{misses} of {results}" if results else None


def rate_ppm(misses: int, results: int) -> int | None:
    """A miss rate in parts per million (saved content holds no floats); None before any."""
    return misses * 1_000_000 // results if results else None


def drop_list(view: Any, spec: PortfolioSpec) -> list[dict[str, Any]]:
    """The dropped items with their reasons, scores and audit results, for the review."""
    result: list[dict[str, Any]] = review(view, spec)["dropped"]
    return result


def review(view: Any, spec: PortfolioSpec) -> dict[str, Any]:
    """Everything the batched review shows beside the ranking: decisions, drops, the audit.

    Reads only artifacts (cards, directives, the round), so any Task's view may compute it.
    """
    keys = items(view, spec) or ()
    scores = {k: spec.card(view, k).get(spec.score) for k in keys}
    worth = {k: spec.worth(view, k) for k in audited(view)}
    return review_of(directives(view), last_round(view), scores, worth,
                     supervised=supervised(view, spec))  # fmt: skip


def read_board(text: str) -> dict[str, Any]:
    """Parse a recitation :func:`render` wrote: counts, bounds, audit, goal and rows.

    For an offline rule model standing in for a live one; a live model reads the text.
    """
    out: dict[str, Any] = {"counts": {}, "bounds": {}, "audit": {}, "goal": "", "rows": []}
    heads = {"WORK BOARD ": "counts", "BOUNDS ": "bounds", "AUDIT ": "audit"}
    for line in text.splitlines():
        head = next((h for h in heads if line.startswith(h)), None)
        if head is not None:
            out[heads[head]] = json.loads(line.removeprefix(head))
        elif line.startswith("GOAL "):
            out["goal"] = line.removeprefix("GOAL ")
        elif line.startswith("{"):
            out["rows"].append(json.loads(line))
    return out
