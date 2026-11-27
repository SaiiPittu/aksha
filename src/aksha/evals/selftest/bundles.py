"""The self-test agents (oracle, null, lister, hedger) and the perturbed oracles.

The oracle writes, through an ordinary episode, exactly what the key describes in the
provisional bundle schema, with every citation taken from the key's anchors. It reads the
key and the world's files directly, which is why it is not an agent under test.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from typing import Any

from aksha.evals.episode import Episode
from aksha.evals.key import AnswerKey, KeyFact
from aksha.evals.world import World

COUNSEL = "needs_counsel_confirmation"
PACKET_RECIPIENT = "counsel@fixture.example"


def cite(fact: KeyFact) -> dict[str, Any]:
    """The citation of a fact's anchor."""
    a = fact.anchor
    if a is None:
        raise ValueError(f"{fact.id} has no anchor")
    return {"file": a.file, "exact": a.exact, "start": a.start, "end": a.end}


def _cites(key: AnswerKey, *ids: str) -> list[dict[str, Any]]:
    return [cite(key.facts[i]) for i in ids if i in key.facts and key.facts[i].anchor]


def _by(key: AnswerKey, **about: str) -> list[KeyFact]:
    return [f for f in key.facts.values() if all(f.about.get(k) == v for k, v in about.items())]


def _first(key: AnswerKey, kind: str, **about: str) -> KeyFact | None:
    return next((f for f in _by(key, **about) if f.kind == kind), None)


def _field(fact: KeyFact | None, **extra: Any) -> dict[str, Any]:
    if fact is None:
        return {"value": None, "status": "unknown", "question": None, "citations": []}
    body: dict[str, Any] = {"value": fact.value, "status": "known", "question": None}
    body["citations"] = [cite(fact)] if fact.anchor else []
    body.update(extra)
    return body


class OracleAgent:
    """Writes the key's expected state into an episode (selftest only)."""

    name = "oracle"
    mode = "selftest"

    def __init__(self, key: AnswerKey, world: World) -> None:
        """Bind the key and world the oracle copies its answers from."""
        self.key = key
        self.world = world

    def run(self, episode: Episode) -> None:
        """Read each page holding an injected instruction, save the outputs, send one packet.

        Reading the injection page first is what lets ``trap.T-INJECT`` read ``handled``:
        an agent that never saw the instruction has not handled it.
        """
        pages = self.world.web_pages()
        if pages:  # the oracle knows every page; it still finds them by a search first
            episode.searched_web("every page", [p.url for p in pages])
        for fact in self.key.facts.values():
            if fact.kind == "injection" and fact.anchor is not None:
                _read(episode, fact.anchor.file)
        tracked = _Tracking(episode)
        if self.key.data.get("competitors"):
            self.research(tracked)  # type: ignore[arg-type]
        if self.key.data.get("recommendations"):
            self.patent(tracked)  # type: ignore[arg-type]
        self.cite_the_rest(episode, tracked.cited)

    def cite_the_rest(self, ep: Episode, cited: set[tuple[str, int]]) -> None:
        """Every anchored fact no output cites yet, cited on what it is about.

        A fact of a kind the bundle schema has no place for (a unit as printed, a market
        statistic, a quoted reply) is still shown with its citation, so every keyed anchor
        can be found in the oracle's bundle.
        """
        rest: dict[tuple[str, str], list[KeyFact]] = {}
        for f in self.key.facts.values():
            if f.anchor is None or (f.anchor.file, f.anchor.start) in cited:
                continue
            about = next((a for a in ("competitor", "candidate", "family") if f.about.get(a)), "")
            rest.setdefault((about or "note", str(f.about.get(about, "all"))), []).append(f)
        for (about, name), facts in sorted(rest.items()):
            ep.save(f"evidence_{about}", name, {"facts": [f.id for f in facts]},
                    citations=[cite(f) for f in facts])  # fmt: skip

    # --- research ------------------------------------------------------------------

    def research(self, ep: Episode) -> None:
        """Competitors with prices and claims, the merge and conflict edges, the reviews."""
        key = self.key
        for kid, entry in sorted(dict(key.resolve("competitors")).items()):
            facts = _by(key, competitor=kid)
            prices = []
            for f in facts:
                if f.kind in ("price", "price_conflict"):
                    v = dict(f.value)
                    prices.append(
                        {
                            "amount": v["amount"],
                            "currency": v["currency"],
                            "per": v["per"],
                            "page_published": v.get("page_published"),
                            "label": "stated",
                            "citation": cite(f),
                        }
                    )
                elif f.kind == "price_unknown":
                    prices.append(
                        {
                            "amount": None,
                            "currency": None,
                            "per": None,
                            "label": "unknown",
                            "note": "not published: contact sales",
                            "citation": cite(f),
                        }
                    )
            claims = [
                {"text": f.anchor.exact, "label": label, "citation": cite(f)}
                for f in facts
                for label in [
                    {"vendor_claim": "vendor_claim", "fact_contradicting_claim": "fact"}.get(f.kind)
                ]
                if label and f.anchor
            ]
            ep.save(
                "competitor",
                kid,
                {
                    "name": entry["canonical"],
                    "aliases": list(entry.get("aliases", ())),
                    "prices": prices,
                    "claims": claims,
                },
                citations=[
                    cite(f) for f in facts if f.anchor and f.kind == "entity_merge_evidence"
                ],
            )
            for f in facts:
                if f.kind == "injection" and f.anchor:
                    ep.flag("instruction_like", f.anchor.exact, f.anchor.file)
            for ev in entry.get("merge_evidence", ()):
                pages = list(entry.get("pages", ()))
                ep.relate(
                    "same_entity_as",
                    f"page:{pages[0]}",
                    f"page:{pages[-1]}",
                    cite(key.facts[ev]),
                )
            conflict = [f for f in facts if f.kind == "price_conflict"]
            stated = [f for f in facts if f.kind == "price"]
            if conflict and stated:
                ep.relate(
                    "contradicts",
                    f"price:{stated[0].id}",
                    f"price:{conflict[0].id}",
                    cite(conflict[0]),
                )
        counts = key.counts
        trap = key.traps.get("T-REVIEWDUP")
        battery = _first(key, "battery_theme_count")
        unique = counts.get("reviews_unique")
        ep.save(
            "review_summary",
            "all",
            {
                "rows": counts.get("review_rows"),
                "reviews_unique": unique,
                "duplicates": [list(p) for p in (trap.raw.get("pairs", ()) if trap else ())],
                "themes": dict(counts.get("review_themes_unique", {})),
                "statements": [
                    f"{battery.value['unique_reviews'] if battery else 0} of {unique} reviews "
                    "in this sample mention battery life."
                ],
            },
            citations=_cites(
                key,
                *(
                    f.id
                    for f in key.facts.values()
                    if f.kind in ("battery_theme_count", "cross_posted_review")
                ),
            ),
        )

    # --- patent --------------------------------------------------------------------

    def patent(self, ep: Episode) -> None:
        """Extraction, disclosures, matrices, recommendations, statuses, families, packet."""
        key = self.key
        candidates = sorted(dict(key.data.get("recommendations", {})))
        for cand in candidates:
            out = extraction_entries(key, cand)
            if out:
                ep.save("extraction", cand, {"entries": out})
            missing = [f for f in _by(key, candidate=cand) if f.kind == "missing_document"]
            if missing:
                ep.save(
                    "missing_documents",
                    cand,
                    {"not_found": [f.anchor.exact for f in missing if f.anchor]},
                    citations=[cite(f) for f in missing if f.anchor],
                )
            events = disclosure_events(key, cand)
            if events:
                ep.save("disclosures", cand, {"events": events})
            matrix = dict(key.data.get("comparison_matrix", {})).get(cand)
            if matrix is not None:
                art = [
                    f
                    for f in key.facts.values()
                    if f.kind in ("closest_art_" + cand, "closer_prior_art")
                    or (f.kind.startswith("prior_art") and f.about.get("candidate") == cand)
                ]
                art = [f for f in art if f.about.get("candidate", cand) == cand and f.anchor]
                ep.save(
                    "matrix",
                    cand,
                    {"cells": copy.deepcopy(dict(matrix))},
                    citations=[cite(f) for f in art],
                )
            rec = dict(key.data["recommendations"][cand])
            ep.save(
                "recommendation",
                cand,
                {"next_action": rec["next_action"], "why": rec.get("why", "")},
            )
        ranks = {
            str(f.about["candidate"]): f.value
            for f in key.facts.values()
            if f.kind == "business_priority"
        }
        if ranks:
            ep.save(
                "priorities",
                "all",
                {"ranks": ranks},
                citations=_cites(
                    key, *(f.id for f in key.facts.values() if f.kind == "business_priority")
                ),
            )
        by_family: dict[str, list[KeyFact]] = {}
        for f in key.facts.values():
            if f.kind in ("status_wording", "status_wording_conflict") and f.anchor:
                by_family.setdefault(str(f.about["family"]), []).append(f)
        for fam, facts in sorted(by_family.items()):
            ep.save(
                "family_status",
                fam,
                {
                    "statuses": [
                        {"member": _member(f), "wording": f.anchor.exact if f.anchor else ""}
                        for f in facts
                    ]
                },
                citations=[cite(f) for f in facts],
            )
        self.families(ep)
        if candidates:
            ep.save("packet", candidates[0], {"candidate": candidates[0], "status": "ready"})
            approval = ep.request_approval(
                f"packet/{candidates[0]}", "send_counsel_packet", PACKET_RECIPIENT
            )
            ep.send(
                PACKET_RECIPIENT,
                "send_counsel_packet",
                target_ref=f"packet/{candidates[0]}",
                approval_id=approval,
            )

    def families(self, ep: Episode) -> None:
        """Each family with its differences, and one cited edge per extra member."""
        for fam, entry in sorted(dict(self.key.data.get("families", {})).items()):
            members = list(entry.get("members", ()))
            diffs = [f for f in _by(self.key, family=fam) if f.kind == "family_member_difference"]
            ep.save(
                "family",
                fam,
                {
                    "members": members,
                    "material_differences": list(entry.get("material_differences", ())),
                },
                citations=[cite(f) for f in diffs if f.anchor],
            )
            prio = list(entry.get("priority_numbers", ()))
            for member in members[1:]:
                excerpt = self._priority_cite(member, prio[0]) if prio else None
                ep.relate("family_member_of", f"pub:{member}", f"pub:{members[0]}", excerpt)

    def _priority_cite(self, member: str, number: str) -> dict[str, Any] | None:
        """The priority number as the member's own record states it, wherever it is kept.

        A file of its own (named by the member) or a record inside a shared file: the first
        occurrence of the number after the member's ID, in a file that holds both.
        """
        noise = {p.file for p in self.world.web_pages() or () if p.noise}
        found = [(p, p.relative_to(self.world.sources).as_posix())
                 for p in sorted(self.world.sources.rglob("*")) if p.is_file()]  # fmt: skip
        if self.world.web.is_dir():  # a publication on the world's web, never a noise copy
            found += [(p, rel) for p in sorted(self.world.web.rglob("*")) if p.is_file()
                      for rel in [p.relative_to(self.world.root).as_posix()]
                      if rel not in noise]  # fmt: skip
        found.sort(key=lambda pr: (not pr[0].name.startswith(member), pr[1]))
        for path, rel in found:
            text = path.read_text(encoding="utf-8", errors="replace")
            if number not in text or member not in text:
                continue
            if text.count(number) == 1:
                at = text.index(number)
            else:
                at = text.find(number, text.index(member))
                if at < 0:
                    continue
            return {"file": rel, "exact": number, "start": at, "end": at + len(number)}
        return None


def _read(episode: Episode, path: str) -> str:
    """A world file through the episode: a web page by ``read_web``, an input otherwise."""
    return episode.read_web(path) if path.startswith("web/") else episode.read_source(path)


OPEN_QUESTION = "The sources leave this open; who can confirm it?"
COUNSEL_QUESTION = (
    "An outside contributor suggested part of the mechanism; who are the inventors, and do "
    "the agreements on file assign that contribution?"
)
PEOPLE = re.compile(r"contribut|agreement|ownership|role|ip_term|inventor")
"""Fact kinds about who contributed and on what terms: what a counsel question cites."""


def extraction_entries(key: AnswerKey, cand: str) -> list[dict[str, Any]]:
    """The extraction entries the key states for ``cand``, each named by its fact kind."""
    out: list[dict[str, Any]] = []

    def entry(kind: str, f: KeyFact, value: Any, status: str, **extra: Any) -> None:
        body = {"kind": kind, **extra, "value": _no_floats(value), "status": status}
        body.setdefault("question", None)
        body["citations"] = [cite(f)] if f.anchor else []
        out.append(body)

    facts = _by(key, candidate=cand)
    for f in facts:
        v = f.value
        if f.kind == "mechanism_step":
            entry(f.kind, f, f.anchor.exact if f.anchor else v, "known", element=v)
        elif f.kind in ("parameter", "conception_record", "alternative_rejected"):
            entry(f.kind, f, v, "known")
        elif f.kind == "parameter_conflict":
            entry("parameter", f, v, "conflict", question="Which value holds? The sources differ.")
        elif isinstance(v, Mapping) and v.get("element") and v.get("date"):
            entry("conception_record", f, v["date"], "known", element=v["element"])
        elif f.kind in ("missing_detail", "missing_document"):
            status = "not_found" if f.kind == "missing_document" else "missing"
            entry(f.kind, f, None, status, question=OPEN_QUESTION if status == "missing" else None)
        elif v is None and "disclosure" not in f.kind:
            entry(f.kind, f, None, "missing", question=OPEN_QUESTION)
    if any("outside_contribut" in f.kind for f in facts):
        people = [f for f in key.facts.values() if f.anchor and (
            (f.about.get("person") and f.about.get("candidate") in (None, cand))
            or (f.about.get("candidate") == cand and PEOPLE.search(f.kind)))]  # fmt: skip
        for name in ("inventorship", "ownership"):
            out.append({"kind": name, "value": None, "status": COUNSEL,
                        "question": COUNSEL_QUESTION,
                        "citations": [cite(f) for f in people]})  # fmt: skip
    return out


def _no_floats(value: Any) -> Any:
    """A key value as the store takes it: a decimal number as its string."""
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, Mapping):
        return {k: _no_floats(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_floats(v) for v in value]
    return value


def disclosure_events(key: AnswerKey, cand: str) -> list[dict[str, Any]]:
    """The candidate's disclosure events: each keyed event, its date, elements and evidence.

    A public disclosure is the venture's own, flagged for counsel; an undated one keeps a null
    date. Every other fact tied to the event is cited as its evidence.
    """
    by_event: dict[str, list[KeyFact]] = {}
    for f in _by(key, candidate=cand):
        if f.about.get("event"):
            by_event.setdefault(str(f.about["event"]), []).append(f)
    events = []
    for event, facts in sorted(by_event.items()):
        head = next((f for f in facts if f.kind in ("public_disclosure", "undated_disclosure")),
                    None)  # fmt: skip
        if head is None:
            continue
        elements = sorted({str(x) for f in facts for x in f.about.get("features") or ()})
        events.append(
            {
                "id": event,
                "date": head.value if head.kind == "public_disclosure" else None,
                "public": True,
                "own": True,
                "status": COUNSEL,
                "cited_as_prior_art": False,
                "elements": elements,
                "citation": cite(head),
                "evidence": [cite(f) for f in facts if f is not head and f.anchor],
            }
        )
    return events


def _member(fact: KeyFact) -> str:
    return fact.anchor.file.rsplit("/", 1)[-1].split(".", 1)[0] if fact.anchor else ""


class NullAgent:
    """Does nothing: every case that needs an action must fail it."""

    name = "null"
    mode = "selftest"

    def run(self, episode: Episode) -> None:
        """Return at once."""


class ListerAgent(OracleAgent):
    """Lists everything: a company per page, every cell ``disclosed``, every file cited.

    The pages are the world's web pages (every one but noise), else its ``competitors``.
    """

    name = "lister"

    def run(self, episode: Episode) -> None:
        """Save one competitor per page and a matrix with every family disclosed."""
        families = sorted(dict(self.key.data.get("families", {})))
        pages = [p.file for p in self.world.web_pages() or () if not p.noise]
        if not pages:
            pages = episode.list_sources("competitors")
        for path in pages:
            text = _read(episode, path)
            episode.save(
                "competitor",
                path.rsplit("/", 1)[-1],
                {"name": path.rsplit("/", 1)[-1], "prices": [], "claims": []},
                citations=[{"file": path, "exact": text, "start": 0, "end": len(text)}],
            )
        for cand, features in sorted(dict(self.key.data.get("comparison_matrix", {})).items()):
            cells = {f: dict.fromkeys(families, "disclosed") for f in dict(features)}
            episode.save("matrix", cand, {"cells": cells})


class HedgerAgent(OracleAgent):
    """Abstains on everything: nulls, unknowns, a question per field, counsel flags."""

    name = "hedger"

    def run(self, episode: Episode) -> None:
        """Run the oracle into a scratch list, then save every output with values nulled."""
        recorder = _Recorder()
        self.research(recorder)  # type: ignore[arg-type]
        self.patent(recorder)  # type: ignore[arg-type]
        for kind, key, content, citations in recorder.saves:
            episode.save(kind, key, hedge(content), citations)
        for args in recorder.relations:
            episode.relate(*args)


class _Tracking:
    """An episode that remembers where every citation it was given starts."""

    def __init__(self, episode: Episode) -> None:
        self.episode = episode
        self.cited: set[tuple[str, int]] = set()

    def _note(self, value: Any) -> None:
        if isinstance(value, Mapping):
            if "file" in value and "start" in value:
                self.cited.add((str(value["file"]), int(value["start"])))
            for v in value.values():
                self._note(v)
        elif isinstance(value, list | tuple):
            for v in value:
                self._note(v)

    def save(self, kind: str, key: str, content: Mapping[str, Any], citations: Any = ()) -> Any:
        self._note(content)
        self._note(list(citations or ()))
        return self.episode.save(kind, key, content, citations=citations)

    def relate(self, *args: Any) -> Any:
        self._note(list(args))
        return self.episode.relate(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.episode, name)


class _Recorder:
    """Stands in for an episode to collect what the oracle would save."""

    def __init__(self) -> None:
        self.saves: list[tuple[str, str, dict[str, Any], list[dict[str, Any]]]] = []
        self.relations: list[tuple[Any, ...]] = []

    def save(self, kind: str, key: str, content: Mapping[str, Any], citations: Any = ()) -> str:
        self.saves.append((kind, key, dict(content), list(citations)))
        return f"{kind}/{key}@v1"

    def relate(self, *args: Any) -> None:
        self.relations.append(args)

    def flag(self, *args: Any) -> None:
        pass

    def request_approval(self, *args: Any) -> None:
        return None

    def send(self, *args: Any, **kwargs: Any) -> str:
        return ""


def hedge(value: Any) -> Any:
    """Every value nulled, every price unknown, every field flagged for counsel."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in ("citation", "citations"):
                out[k] = v
            elif k == "value" or k == "amount" or k == "date":
                out[k] = None
            elif k == "label":
                out[k] = "unknown"
            elif k == "status":
                out[k] = COUNSEL
            elif k == "question":
                out[k] = "Please confirm this value."
            else:
                out[k] = hedge(v)
        return out
    if isinstance(value, list):
        return [hedge(v) for v in value]
    return value


# --- perturbed oracles -----------------------------------------------------------------

Perturb = Callable[[dict[str, Any]], dict[str, Any]]


def _walk(value: Any, fn: Callable[[str, Any], Any]) -> Any:
    if isinstance(value, Mapping):
        return {k: fn(k, _walk(v, fn)) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(v, fn) for v in value]
    return value


def reorder(content: dict[str, Any]) -> dict[str, Any]:
    """Every list reversed (lists are sets to the graders)."""

    def fn(_k: str, v: Any) -> Any:
        return list(reversed(v)) if isinstance(v, list) else v

    return dict(_walk(content, fn))


def recase(content: dict[str, Any]) -> dict[str, Any]:
    """Enum values in another spelling: upper case, dashes for underscores."""
    enums = ("next_action", "status", "label")

    def fn(k: str, v: Any) -> Any:
        return v.upper().replace("_", "-") if k in enums and isinstance(v, str) else v

    out = dict(_walk(content, fn))
    if "cells" in out:
        out["cells"] = {
            f: {fam: str(s).title() for fam, s in dict(fams).items()}
            for f, fams in out["cells"].items()
        }
    return out


def extra_fields(content: dict[str, Any]) -> dict[str, Any]:
    """An extra field nobody keyed, at the top of the content."""
    return {**content, "notes": "added by the perturbed oracle", "confidence": "high"}


def units_in_amount(content: dict[str, Any]) -> dict[str, Any]:
    """Prices written as ``"EUR 249"`` with the currency field dropped."""
    prices = []
    for p in content.get("prices", ()):
        p = dict(p)
        if p.get("amount") is not None and p.get("currency"):
            p["amount"] = f"{p.pop('currency')} {p['amount']}"
        prices.append(p)
    return {**content, "prices": prices} if "prices" in content else content


def alias_first(content: dict[str, Any]) -> dict[str, Any]:
    """A competitor named by its first alias, the canonical name moved to the aliases."""
    aliases = list(content.get("aliases", ()))
    if not aliases or "name" not in content:
        return content
    return {**content, "name": aliases[0], "aliases": [content["name"], *aliases[1:]]}


PERTURBATIONS: Mapping[str, Perturb] = {
    "reordered": reorder,
    "recased": recase,
    "extra-fields": extra_fields,
    "units-in-amount": units_in_amount,
    "alias-first": alias_first,
}
