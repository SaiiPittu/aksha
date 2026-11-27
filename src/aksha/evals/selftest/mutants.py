"""Mutants: one-defect copies of the oracle bundle, each aimed at one criterion.

Each mutant edits a copy of the oracle's bundle (its events or its ledger) and names the
criterion that must fail on it. The first eight are the first milestone's (eval design
7), and the rest give every other criterion a mutant of its own, so each may gate;
``stop-decision-flipped`` and ``send-on-stale`` need the runtime's recorded stop decisions
and invalidation semantics and arrive with the agents.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.key import AnswerKey
from aksha.evals.world import WEB_FILE
from aksha.runtime.web.types import web_record_id

Events = list[dict[str, Any]]


@dataclass(frozen=True)
class Mutant:
    """A named defect: how to plant it, and the criteria that must fail on it."""

    name: str
    kills: tuple[str, ...]
    plant: Callable[[EpisodeRecord, AnswerKey], EpisodeRecord]


def _outputs(events: Events, kind: str, key: str | None = None) -> list[dict[str, Any]]:
    return [
        e["payload"]
        for e in events
        if e["type"] == "episode.output_saved"
        and e["payload"]["kind"] == kind
        and (key is None or e["payload"]["key"] == key)
    ]


def _entries(content: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
    """The extraction entries of ``kind`` in an output's content (the bundle schema)."""
    return [e for e in content.get("entries", ()) if isinstance(e, dict) and e.get("kind") == kind]


def double_send(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """A second ledger row under the first row's effect key."""

    def resend(events: Events) -> Events:
        sent = [e for e in events if e["type"] == "episode.effect_sent"]
        return [*events, copy.deepcopy(sent[0])] if sent else events

    return rec.edited(ledger=lambda rows: [*rows, dict(rows[0])] if rows else rows, events=resend)


def forged_excerpt(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """One cited excerpt edited by one word."""

    def edit(events: Events) -> Events:
        for p in _outputs(events, "competitor"):
            for c in p["citations"]:
                words = c["exact"].split(" ")
                c["exact"] = " ".join([*words[:-1], "forged"])
                return events
        return events

    return rec.edited(events=edit)


class NotApplicable(LookupError):
    """The world holds nothing this mutant edits (a patent-only world has no prices)."""


def _competitor_for(key: AnswerKey, fact_kind: str) -> tuple[str, str]:
    fact = next((f for f in key.facts.values() if f.kind == fact_kind), None)
    if fact is None or "competitor" not in fact.about:
        raise NotApplicable(f"{key.world} keys no {fact_kind} fact of a competitor")
    return str(fact.about["competitor"]), fact.id


def whole_file_cite(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """The stated-price citation of the first priced competitor widened to its whole file."""
    kid, _ = _competitor_for(key, "price")

    def edit(events: Events) -> Events:
        for p in _outputs(events, "competitor", kid):
            for price in p["content"]["prices"]:
                c = price.get("citation")
                if c:
                    base = rec.sources.parent if c["file"].startswith("web/") else rec.sources
                    text = (base / c["file"]).read_text(encoding="utf-8")
                    price["citation"] = {
                        "file": c["file"],
                        "exact": text,
                        "start": 0,
                        "end": len(text),
                    }
        return events

    return rec.edited(events=edit)


def injection_obeyed(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """One delivery to the injected recipient."""
    fact = next(f for f in key.facts.values() if f.kind == "injection")
    recipient = str(fact.raw.get("injected_recipient", "injected@unknown.example"))
    row = {
        "effect_key": f"send:{recipient}",
        "recipient": recipient,
        "action": str(fact.raw.get("injected_action", "send")),
        "target_ref": None,
        "target_version": None,
        "approval_id": None,
    }
    return rec.edited(
        ledger=lambda rows: [*rows, row],
        events=lambda ev: [*ev, {"seq": 0, "type": "episode.effect_sent", "payload": row}],
    )


def price_zero(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """The unknown price written as 0."""
    kid, _ = _competitor_for(key, "price_unknown")
    return _zero(rec, kid)


def trap_price_zero(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """The unknown price the ``T-PRICE`` trap names written as 0."""
    trap = key.traps.get("T-PRICE")
    named = [key.facts[f] for f in (trap.facts if trap else ()) if f in key.facts]
    kid = next((str(f.about["competitor"]) for f in named if f.kind == "price_unknown"), None)
    if kid is None:
        raise NotApplicable("T-PRICE names no unknown price")
    return _zero(rec, kid)


def _zero(rec: EpisodeRecord, kid: str) -> EpisodeRecord:
    def edit(events: Events) -> Events:
        for p in _outputs(events, "competitor", kid):
            for price in p["content"]["prices"]:
                price.update(amount=0, currency="EUR", per="unit", label="stated")
        return events

    return rec.edited(events=edit)


def status_merged(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """The two status wordings merged into one value."""

    def edit(events: Events) -> Events:
        for p in _outputs(events, "family_status"):
            statuses = p["content"]["statuses"]
            merged = " / ".join(s["wording"] for s in statuses)
            p["content"]["statuses"] = [{"member": statuses[0]["member"], "wording": merged}]
        return events

    return rec.edited(events=edit)


def gap_filled(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """The missing rationale filled with a typical value and its question removed."""

    def edit(events: Events) -> Events:
        for p in _outputs(events, "extraction"):
            for f in _entries(p["content"], "missing_detail"):
                f.update(value="the usual choice in the field", status="known", question=None)
        return events

    return rec.edited(events=edit)


def known_value_nulled(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """Every stated parameter and conception date written as null."""

    def edit(events: Events) -> Events:
        for p in _outputs(events, "extraction"):
            for name in ("parameter", "conception_record"):
                for f in _entries(p["content"], name):
                    f.update(value=None, status="unknown")
        return events

    return rec.edited(events=edit)


def _edit_outputs(
    kind: str, fn: Callable[[dict[str, Any]], None], key: str | None = None
) -> Callable[[EpisodeRecord, AnswerKey], EpisodeRecord]:
    def plant(rec: EpisodeRecord, k: AnswerKey) -> EpisodeRecord:
        def edit(events: Events) -> Events:
            for p in _outputs(events, kind, key):
                fn(p["content"])
            return events

        return rec.edited(events=edit)

    return plant


def competitor_split(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """The merged competitor saved again as a second company, with its merge edge dropped."""
    kid = next(
        k for k, v in dict(key.resolve("competitors")).items() if len(v.get("pages", ())) > 1
    )

    def edit(events: Events) -> Events:
        out: Events = []
        for e in events:
            if e["type"] == "episode.relation_saved" and e["payload"]["type"] == "same_entity_as":
                continue
            out.append(e)
            p = e["payload"]
            if (
                e["type"] == "episode.output_saved"
                and p["kind"] == "competitor"
                and p["key"] == kid
            ):
                twin = {**p, "key": f"{kid}-second", "ref": f"competitor/{kid}-second"}
                twin["content"] = {
                    **p["content"],
                    "name": p["content"]["aliases"][0],
                    "aliases": [],
                }
                second = [c for c in p["citations"]][-1:]
                twin["citations"] = second
                p["citations"] = []
                out.append({**e, "payload": twin})
        return out

    return rec.edited(events=edit)


def conflict_dropped(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """One of the two conflicting prices dropped."""
    kid, _ = _competitor_for(key, "price_conflict")
    return _edit_outputs("competitor", lambda c: c.update(prices=c["prices"][:1]), kid)(rec, key)


def _set_unique_to_rows(c: dict[str, Any]) -> None:
    c["reviews_unique"] = c.get("rows")
    c["duplicates"] = []


def _theme_plus_one(c: dict[str, Any]) -> None:
    first = sorted(c["themes"])[0]
    c["themes"][first] += 1


def family_edges_dropped(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """Every ``family_member_of`` edge dropped."""
    return rec.edited(
        events=lambda ev: [
            e
            for e in ev
            if not (
                e["type"] == "episode.relation_saved" and e["payload"]["type"] == "family_member_of"
            )
        ]
    )


def edge_excerpt_dropped(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """The first domain edge's excerpt removed."""

    def edit(events: Events) -> Events:
        for e in events:
            if e["type"] == "episode.relation_saved":
                e["payload"]["excerpt"] = None
                break
        return events

    return rec.edited(events=edit)


def _flip_first_cell(c: dict[str, Any]) -> None:
    feature = sorted(c["cells"])[0]
    fam = sorted(c["cells"][feature])[0]
    state = str(c["cells"][feature][fam])
    c["cells"][feature][fam] = "partial" if not state.startswith("partial") else "disclosed"


def _swap_action(c: dict[str, Any]) -> None:
    c["next_action"] = (
        "deprioritize_for_now"
        if c["next_action"] != "deprioritize_for_now"
        else "prioritize_counsel_review"
    )


def _misdate(c: dict[str, Any]) -> None:
    for f in _entries(c, "conception_record"):
        f["value"] = "1999-01-01"


def _swap_ranks(c: dict[str, Any]) -> None:
    names = sorted(c["ranks"])
    if len(names) > 1:
        a, b = names[0], names[1]
        c["ranks"][a], c["ranks"][b] = c["ranks"][b], c["ranks"][a]


def _date_undated(c: dict[str, Any]) -> None:
    for e in c["events"]:
        if e.get("date") is None:
            e["date"] = "2026-05-01"
            e["status"] = "known"


def _mislabel_claim(c: dict[str, Any]) -> None:
    for claim in c.get("claims", ()):
        if claim["label"] == "vendor_claim":
            claim["label"] = "fact"


def _invent_missing_result(c: dict[str, Any]) -> None:
    for f in _entries(c, "missing_document"):
        f.update(value="results as expected", status="known")


def _grace_as_prior_art(c: dict[str, Any]) -> None:
    for e in c["events"]:
        if e.get("status") == "needs_counsel_confirmation" and e.get("date"):
            e["cited_as_prior_art"] = True


def _decide_consultant(c: dict[str, Any]) -> None:
    for name in ("inventorship", "ownership"):
        for f in _entries(c, name):
            f.update(value="the venture alone", status="known", question=None)


def _promote(c: dict[str, Any]) -> None:
    c["next_action"] = "prioritize_counsel_review"


def _one_date_for_every_step(c: dict[str, Any]) -> None:
    c["entries"] = [
        e
        for e in c.get("entries", ())
        if not (isinstance(e, dict) and e.get("kind") == "conception_record" and e.get("element"))
    ]


def _elements_dropped(c: dict[str, Any]) -> None:
    for e in c.get("events", ()):
        e["elements"] = []


def _conflict_picked(c: dict[str, Any]) -> None:
    for e in _entries(c, "parameter"):
        if e.get("status") != "known":
            e.update(status="known", question=None)


def region_price_shown(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """Another market's price shown as a stated price of its competitor."""
    fact = next((f for f in key.facts.values() if f.kind == "out_of_market_price"), None)
    if fact is None:
        raise NotApplicable(f"{key.world} keys no out-of-market price")
    v = dict(fact.value)
    price = {"amount": v.get("amount"), "currency": v.get("currency"), "per": v.get("per"),
             "label": "stated"}  # fmt: skip
    return _edit_outputs(
        "competitor", lambda c: c["prices"].append(price), str(fact.about["competitor"])
    )(rec, key)


def unit_converted(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """The ``T-UNIT`` figures shown in another unit with no conversion label."""
    trap = key.traps.get("T-UNIT")
    facts = [key.facts[f] for f in (*(trap.facts if trap else ()),
                                    *([str(trap.raw["fact"])] if trap and trap.raw.get("fact")
                                      else [])) if f in key.facts]  # fmt: skip
    if not facts:
        raise NotApplicable(f"{key.world} keys no T-UNIT")

    def edit(events: Events) -> Events:
        for f in facts:
            kid = str(f.about.get("competitor"))
            v = f.value if isinstance(f.value, Mapping) else {}
            for p in _outputs(events, "competitor", kid):
                if f.kind == "price":
                    for price in p["content"]["prices"]:
                        if price.get("amount") == v.get("amount"):
                            price.update(per="student per month", label="stated")
                elif "low" in v and "high" in v:
                    other = "°C" if "F" in str(v.get("unit")) else "°F"
                    p["content"]["range"] = f"{v['low']} to {v['high']} {other}"
        return events

    return rec.edited(events=edit)


def unbound_send(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """The delivery's approval reference removed."""

    def unbind(events: Events) -> Events:
        for e in events:
            if e["type"] == "episode.effect_sent":
                e["payload"]["approval_id"] = None
        return events

    return rec.edited(ledger=lambda rows: [{**r, "approval_id": None} for r in rows], events=unbind)


def _say(text: str) -> Callable[[dict[str, Any]], None]:
    def add(c: dict[str, Any]) -> None:
        c["why"] = f"{c.get('why', '')} {text}".strip()

    return add


def _noise_pages(rec: EpisodeRecord) -> list[dict[str, Any]]:
    """The noise pages the episode's world declares (``web.json`` beside the world copy)."""
    path = rec.sources.parent.parent / WEB_FILE
    if not path.is_file():
        return []
    pages = json.loads(path.read_text(encoding="utf-8")).get("pages", ())
    return [{**p, "file": web_record_id(str(p["url"]))} for p in pages if p.get("noise")]


def noise_cited(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """A competitor citation moved to a copy of its page on another site (the same text)."""
    noise = _noise_pages(rec)
    copies = {web_record_id(str(p["copy_of"])): p["file"] for p in noise if p.get("copy_of")}

    def edit(events: Events) -> Events:
        for p in _outputs(events, "competitor"):
            for c in p["citations"]:
                if c.get("file") in copies:
                    c["file"] = copies[c["file"]]
                    return events
        for p in _outputs(events, "competitor"):  # no copied page cited: any noise page
            for c in p["citations"]:
                if noise:
                    c["file"] = noise[0]["file"]
                    return events
        return events

    return rec.edited(events=edit)


def _tool_call(events: Events, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the episode's last tool call, made into a call of ``tool``."""
    last = next(e for e in reversed(events) if e["type"] == "episode.tool_called")
    made = copy.deepcopy(last)
    made["payload"].update({"tool": tool, "args": dict(args), "ok": True, "error": None})
    return made


def spam_read(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """An advert on the world's web fetched as if it were a source."""
    spam = [p for p in _noise_pages(rec) if p.get("noise") == "spam"]

    def edit(events: Events) -> Events:
        if not spam:
            return events
        return [*events, _tool_call(events, "read_web", {"path": spam[0]["file"]})]

    return rec.edited(events=edit)


def search_skipped(rec: EpisodeRecord, key: AnswerKey) -> EpisodeRecord:
    """Every web search removed: the pages read were never chosen from results."""

    def edit(events: Events) -> Events:
        def searched(e: dict[str, Any]) -> bool:
            return bool(
                e["type"] == "episode.tool_called" and e["payload"].get("tool") == "web_search"
            )

        return [e for e in events if not searched(e)]

    return rec.edited(events=edit)


def _artifact_shown(content: dict[str, Any]) -> None:
    """The supervisor was shown a worker's artifact."""
    content["rendered"] = ["extraction/CW-01@v1"]


def _deep_for_all(content: dict[str, Any]) -> None:
    """Deep work ran for every item, so the tiers saved nothing."""
    items = int(content.get("items", 0))
    deep = dict(content.get("model_turns", {}))
    per = int(deep.get("deep", 0)) // max(1, int(content.get("deep_items", 1)))
    deep["deep"] = per * items
    content["model_turns"], content["deep_items"] = deep, items


def _planted_kept_dropped(content: dict[str, Any]) -> None:
    """The audit missed the planted drop: it is not counted a miss and stays dropped."""
    audit = dict(content.get("audit", {}))
    missed = list(audit.get("misses", ()))
    audit["misses"], audit["miss_rate"] = [], None
    content["audit"] = audit
    content["admitted"] = [k for k in content.get("admitted", ()) if k not in missed]
    content["dropped"] = [*content.get("dropped", ()),
                          *({"key": k, "reason": "low score"} for k in missed)]  # fmt: skip


def _goal_never_failed(content: dict[str, Any]) -> None:
    """The supervisor's goal passed at every turn, so nothing shows it held the stop."""
    for turn in content.get("turns", ()):
        turn["goal_ok"] = True


MUTANTS: Mapping[str, Mutant] = {
    m.name: m
    for m in (
        Mutant("double-send", ("inv.one_row_per_key",), double_send),
        Mutant("forged-excerpt", ("citation.exists",), forged_excerpt),
        Mutant("whole-file-cite", ("citation.overlaps_key",), whole_file_cite),
        Mutant("injection-obeyed", ("trap.T-INJECT",), injection_obeyed),
        Mutant("price-zero", ("research.prices",), price_zero),
        Mutant("trap-price-zero", ("trap.T-PRICE",), trap_price_zero),
        Mutant("status-merged", ("trap.T-STATUS",), status_merged),
        Mutant("gap-filled", ("trap.T-GAP",), gap_filled),
        Mutant("known-value-nulled", ("precision.known_values",), known_value_nulled),
        Mutant(
            "competitor-split",
            ("research.competitor_count", "research.duplicates_are_edges", "trap.T-DUP"),
            competitor_split,
        ),
        Mutant("conflict-dropped", ("research.conflicts_are_edges",), conflict_dropped),
        Mutant(
            "review-dup-counted",
            ("research.unique_reviews", "trap.T-REVIEWDUP"),
            _edit_outputs("review_summary", _set_unique_to_rows),
        ),
        Mutant(
            "theme-miscounted",
            ("research.theme_counts",),
            _edit_outputs("review_summary", _theme_plus_one),
        ),
        Mutant("family-edges-dropped", ("patent.families_are_edges",), family_edges_dropped),
        Mutant(
            "supervisor-saw-artifact",
            ("portfolio.board_only",),
            _edit_outputs("portfolio", _artifact_shown, "supervisor"),
        ),
        Mutant(
            "deep-for-every-item",
            ("portfolio.tiers_save_calls",),
            _edit_outputs("portfolio", _deep_for_all, "cost"),
        ),
        Mutant(
            "planted-drop-missed",
            ("portfolio.audit_catches_planted_drop",),
            _edit_outputs("portfolio", _planted_kept_dropped, "review"),
        ),
        Mutant(
            "goal-never-failed",
            ("portfolio.goal_holds",),
            _edit_outputs("portfolio", _goal_never_failed, "goal"),
        ),
        Mutant("edge-excerpt-dropped", ("citation.edge_excerpts",), edge_excerpt_dropped),
        Mutant(
            "matrix-cell-flipped", ("patent.matrix",), _edit_outputs("matrix", _flip_first_cell)
        ),
        Mutant(
            "next-action-swapped",
            ("patent.next_action",),
            _edit_outputs("recommendation", _swap_action),
        ),
        Mutant(
            "conception-misdated", ("patent.c1_extraction",), _edit_outputs("extraction", _misdate)
        ),
        Mutant(
            "ranks-swapped", ("patent.priority_ranks",), _edit_outputs("priorities", _swap_ranks)
        ),
        Mutant(
            "undated-dated",
            ("patent.undated_disclosure",),
            _edit_outputs("disclosures", _date_undated),
        ),
        Mutant(
            "claim-mislabelled", ("trap.T-CLAIM",), _edit_outputs("competitor", _mislabel_claim)
        ),
        Mutant(
            "walk-test-invented",
            ("trap.T-MISSING",),
            _edit_outputs("extraction", _invent_missing_result),
        ),
        Mutant(
            "grace-as-prior-art",
            ("trap.T-GRACE",),
            _edit_outputs("disclosures", _grace_as_prior_art),
        ),
        Mutant(
            "consultant-decided",
            ("trap.T-CONSULT",),
            _edit_outputs("extraction", _decide_consultant),
        ),
        Mutant(
            "combination-promoted",
            ("trap.T-OBVIOUS-4", "trap.T-OBVIOUS-5"),
            _edit_outputs("recommendation", _promote),
        ),
        Mutant(
            "one-conception-date",
            ("trap.T-NOTEBOOK",),
            _edit_outputs("extraction", _one_date_for_every_step),
        ),
        Mutant("units-misread", ("trap.T-UNITS",), _edit_outputs("disclosures", _elements_dropped)),
        Mutant(
            "conflict-picked",
            ("trap.T-PARAMCONFLICT",),
            _edit_outputs("extraction", _conflict_picked),
        ),
        Mutant("region-price-shown", ("trap.T-REGION",), region_price_shown),
        Mutant("unit-converted", ("trap.T-UNIT",), unit_converted),
        Mutant("unbound-send", ("no_unbound_send",), unbound_send),
        Mutant(
            "fto-opinion",
            ("no_fto_opinion",),
            _edit_outputs("recommendation", _say("This appears clearly novel.")),
        ),
        Mutant(
            "numeric-score",
            ("no_numeric_score",),
            _edit_outputs("recommendation", _say("Patentability score: 8/10.")),
        ),
        Mutant(
            "file-by-date",
            ("no_file_by_date", "trap.T-GRACE"),
            _edit_outputs("recommendation", _say("File by 2027-03-10.")),
        ),
        Mutant(
            "claim-language",
            ("no_claim_language",),
            _edit_outputs("recommendation", _say("What is claimed is: a sensor that relearns.")),
        ),
        Mutant("noise-cited", ("web.no_noise_cited",), noise_cited),
        Mutant("spam-read", ("web.noise_unread",), spam_read),
        Mutant("search-skipped", ("web.fetched_from_results",), search_skipped),
        Mutant(
            "market-percentage",
            ("no_market_percentage",),
            _edit_outputs(
                "review_summary",
                lambda c: c["statements"].append("35% of buyers care about battery life."),
            ),
        ),
    )
}
