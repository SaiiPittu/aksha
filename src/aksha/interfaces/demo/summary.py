"""The demo's printed summary: what ran, what changed, what was reused, and the counts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from .story import BY, Report


def _slots(refs: Iterable[str]) -> str:
    """``patent.comparison/C2@v1`` and friends as ``C2: comparison, ...; table``."""
    by_key: dict[str, list[str]] = defaultdict(list)
    for ref in refs:
        slot = ref.split("@", 1)[0]
        kind, _, key = slot.partition("/")
        by_key[key or "-"].append(kind.split(".", 1)[1])
    parts = [f"{k}: {', '.join(v)}" for k, v in sorted(by_key.items()) if k != "-"]
    if "-" in by_key:
        parts.append(", ".join(by_key["-"]))
    return "; ".join(parts)


def render(report: Report, work: str) -> str:
    """The summary as plain text, about thirty lines."""
    acts = dict(report.acts)
    runs = report.runs
    rebuilds = [r for r, v in runs.items() if v["cause"] == "rebuild"]
    c, k, m, n = report.change, report.kill, report.mailbox, report.next_action
    kept_research, kept_patent = report.kept["research"], report.kept["patent"]
    first_approval, fresh_approval = report.approvals[0], report.approvals[-1]
    lines = [
        "Aksha demo: change and recovery. Offline, fake models, no keys.",
        f"Every answer and approval a person would give is scripted (logged by={BY!r}).",
        "",
        f"1. Both agents run on the stillroom world ({acts['first runs']} s)",
    ]
    for run, v in runs.items():
        if v["cause"] == "request":
            lines.append(f"   {v['agent']:<8} {run} {v['outcome']}: "
                         f"{len(report.first[v['agent']])} outputs, "
                         f"{v['model_calls']} model calls")  # fmt: skip
    lines += [f"   scripted: {a}" for a in report.asked]
    lines += [
        f"   scripted: approved {first_approval['approval']} for "
        f"{first_approval['targets'][0]}; the mailbox received it once",
        "",
        f"2. The input changes to stillroom-v2's change set ({acts['change']} s)",
        f"   added {', '.join(c['added'])} (a new publication family)",
        f"   edited {', '.join(c['edited'])} (the outside suggestion, now dated earlier)",
        f"   one command recorded {len(c['records_moved'])} moved records and re-ran "
        f"{c['queries_rerun']} recorded searches and listings: {c['queries_changed']} found "
        "new hits",
        "",
        f"3. Invalidation reopens only what depends on the change ({acts['rebuild']} s)",
        f"   reached {len(c['invalidated'])} invalidated + {len(c['dirty'])} downstream: "
        f"{_slots(c['invalidated'] + c['dirty'])}",
        f"   rebuilt {len(report.rebuilt)}: {_slots(report.rebuilt)}",
        f"   kept {len(kept_patent)} patent outputs: "
        f"{_slots(report.first['patent'][s] for s in kept_patent)}",
        f"   kept all {len(kept_research)} research outputs: nothing they read moved",
        f"   early cutoff: the {c['queries_rerun'] - c['queries_changed']} queries that "
        f"found the same hits stopped the cascade; "
        f"{len(report.cutoff)} rebuilt version came out equal",
        f"   C1 next action: {n['before']} -> {n['after']} "
        f"(the v2 answer key expects {n['key_expects']})",
        f"   scripted: accepted the packet's diff review, {report.reviewed}",
        f"   scripted: approved the fresh request {fresh_approval['approval']} for "
        f"{fresh_approval['targets'][0]}",
        "   rebuild runs: "
        + ", ".join(f"{r} ({runs[r]['model_calls']} model calls)" for r in rebuilds)
        + "; the research agent opened none",
        "",
        f"4. SIGKILL mid-send, then recovery ({acts['kill and recover']} s)",
        f"   a child process sent {fresh_approval['targets'][0]}; the mailbox stored it, "
        f"then SIGKILL (exit {k['returncode']}) before the receipt was recorded",
        f"   at restart: {k['outbox_open_at_restart']} outbox entry open with no receipt; "
        f"recovery asked the mailbox by key {k['recovery_lookups']} time, sent "
        f"{k['recovery_applies']} times; {rebuilds[-1]} ended {runs[rebuilds[-1]]['outcome']}",
        f"   mailbox: {m['deliveries']} deliveries for {m['keys']} idempotency keys "
        f"({', '.join(m['packets'])}): each packet version exactly once",
        "",
        "5. Event store",
        f"   hash chain {'verified' if report.chain['ok'] else 'BROKEN'}: "
        f"{report.chain['events']} events, head {report.chain['head'][:23]}...",
        "",
        f"Done in {report.seconds} s. Store, mailbox and sources: {work}",
    ]
    return "\n".join(lines)
