"""The research agent's offline model: a rule-based generation policy (ADR-P07 fakes).

:class:`ResearchModel` reads only what its request carries: the request text, the read
block of the artifacts its Task declares, the person's answers, and its own earlier tool
calls with their results (each result followed by the span IDs it may cite). From that it
picks the next calls of its Task by fixed rules, so a run in ``fake`` mode is offline and
writes the same bytes twice. The judgement it applies is in ``analysis.py``; this module
only turns tool results into calls and saved content.

Text inside a fetched page is data: a passage flagged ``instruction_like`` is recorded as
a flag and never changes a call. :class:`ObedientModel` is the counter-example for tests:
it does what such a passage says, and the policy gate refuses it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aksha.runtime.api import (
    GenerationRequest,
    GenerationResult,
    InstanceRef,
    RuleGenerationModel,
    ToolCall,
)

from . import analysis

PAGES, REVIEWS, NOTES = "competitors", "reviews", "priorities"
CELLS = ("unit_price", "subscription", "power", "battery_life", "privacy")
DISCOVER_TERMS = ("occupancy", "presence", "people counter")
MANUAL_TERMS = ("spreadsheet", "manual count", "walk-round", "badge data", "clipboard")
SEGMENTS = ("care homes", "offices", "office", "homes", "retail", "hospitals", "schools")
GEOGRAPHIES: Mapping[str, str] = {
    "eu": "EU",
    "europe": "EU",
    "european": "EU",
    "germany": "DE",
    "netherlands": "NL",
    "france": "FR",
    "spain": "ES",
    "uk": "UK",
    "united kingdom": "UK",
    "us": "US",
    "usa": "US",
    "united states": "US",
    "india": "IN",
    "global": "global",
    "worldwide": "global",
}
EU = frozenset({"DE", "NL", "FR", "ES", "IT", "BE", "AT", "IE", "PL", "SE", "DK", "FI", "PT"})
REGION_MEMBERS: Mapping[str, frozenset[str]] = {"EU": EU}
ASK_SEGMENT = (
    "Which buyers is this for: offices, homes or retail? It decides which competitors count."
)
ASK_GEOGRAPHY = (
    "Which geography should this cover? Prices are listed in EUR and USD and reviews come "
    "from several countries, so it changes what counts."
)
BOUNDARY_IN = "products that do the job the question names, and the manual ways buyers use today"

_ARTIFACT = re.compile(r"^\[artifact ([^,\]]+), (full|summary)\]\n(.*)$", re.S)
_UNKNOWN = re.compile(r"^\[unknown (\S+): (\w+)(?:, blocked by (.+?))?(?:, failed steps (.+?))?\]$")
_ANSWER = re.compile(
    r"Q: (.*?)\nA: (.*?)(?:\n\(cite this answer as statement (EX-\d+)\))?(?=\nQ: |\Z)", re.S
)


@dataclass(frozen=True)
class Result:
    """One earlier call and what it returned: ``data`` on success, ``error`` otherwise."""

    step: str
    name: str
    args: Mapping[str, Any]
    data: Mapping[str, Any] | None
    error: Mapping[str, Any] | None
    cites: tuple[str, ...] = ()

    def cite(self, n: int, text: str) -> dict[str, str]:
        """The cite of passage ``n`` of this result, quoting ``text`` (the whole passage)."""
        return {"span": self.cites[n], "quote": text}


@dataclass
class Seen:
    """What one request carries, parsed (module doc)."""

    ref: InstanceRef
    turn: int
    request: str = ""
    artifacts: dict[str, tuple[str, Mapping[str, Any]]] = field(default_factory=dict)
    unknowns: list[dict[str, str]] = field(default_factory=list)
    answers: list[tuple[str, str, str | None]] = field(default_factory=list)
    results: list[Result] = field(default_factory=list)

    def of(self, name: str) -> list[Result]:
        """Earlier results of tool ``name``, oldest first."""
        return [r for r in self.results if r.name == name]

    def artifact(self, kind: str) -> tuple[str, Mapping[str, Any]] | None:
        """``(ref, content)`` of the read slot ``kind`` (``research.scope``), if rendered."""
        return self.artifacts.get(kind)


def parse(request: GenerationRequest) -> Seen:
    """Read ``request`` into :class:`Seen` (module doc)."""
    run, _, rest = request.step_id.partition("/")
    instance, _, turn = rest.rpartition("/")
    seen = Seen(InstanceRef.parse(run, instance), int(turn.lstrip("t") or 1))
    calls: dict[str, tuple[str, Mapping[str, Any]]] = {}
    turn_no = 0
    for m in request.messages:
        if m.role == "user" and m.text:
            _user(seen, m.text)
        elif m.role == "assistant":
            turn_no += 1
            for i, c in enumerate(m.tool_calls, start=1):
                step = f"{run}/{instance}/t{turn_no:02d}/c{i:02d}"
                calls[c.call_id] = (step, c.name, c.arguments)  # type: ignore[assignment]
        elif m.role == "tool" and m.call_id in calls and m.text:
            step, name, args = calls[m.call_id]  # type: ignore[misc]
            seen.results.append(_result(step, name, args, m.text))
    return seen


def _user(seen: Seen, text: str) -> None:
    if text.startswith("Artifacts this Task reads"):
        for block in text.split("\n\n")[1:]:
            a = _ARTIFACT.match(block)
            if a is not None:
                ref = a.group(1)
                slot = ref.rsplit("@", 1)[0]
                seen.artifacts[slot] = (ref, json.loads(a.group(3)))
                continue
            for line in block.splitlines():
                u = _UNKNOWN.match(line.strip())
                if u is not None:
                    seen.unknowns.append(
                        {
                            "slot": u.group(1),
                            "reason": u.group(2),
                            "blocker": u.group(3) or "",
                            "failed": u.group(4) or "",
                        }
                    )
    elif text.startswith("Answers from the person:"):
        body = text.split("\n", 1)[1]
        seen.answers = [(q, a, s) for q, a, s in _ANSWER.findall(body)]
        seen.answers = [(q, a, s or None) for q, a, s in seen.answers]
    elif not seen.request:
        seen.request = text.split("\n\nTask: ", 1)[0]


def _result(step: str, name: str, args: Mapping[str, Any], text: str) -> Result:
    head, _, tail = text.partition("\n")
    cites: tuple[str, ...] = ()
    if tail.startswith('{"citation_spans"'):
        cites = tuple(json.loads(tail)["citation_spans"])
    try:
        shown = json.loads(head)
    except ValueError:
        return Result(step, name, args, None, {"code": "unreadable"})
    if shown.get("status") == "ok":
        return Result(step, name, args, shown.get("data", {}), None, cites)
    return Result(step, name, args, None, shown, cites)


def _calls(seen: Seen, calls: Sequence[tuple[str, Mapping[str, Any]]]) -> GenerationResult:
    out = tuple(ToolCall(f"t{seen.turn}-{i}", n, a) for i, (n, a) in enumerate(calls, start=1))
    return GenerationResult(None, out, "tool_calls")


def _save(
    kind: str,
    content: Mapping[str, Any],
    key: str | None = None,
    relations: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, dict[str, Any]]:
    args: dict[str, Any] = {"kind": kind, "content": dict(content)}
    if key is not None:
        args["key"] = key
    if relations:
        args["relations"] = list(relations)
    return ("save_artifact", args)


def _finish(summary: str) -> tuple[str, dict[str, Any]]:
    return ("finish", {"summary": summary})


def _record_ref(data: Mapping[str, Any]) -> dict[str, str]:
    return {"kind": "record", "id": str(data["record"]), "version": str(data["version"])}


def _unknown(
    question: str, reason: str, *, checked: Sequence[str] = (), failed_step: str | None = None
) -> dict[str, Any]:
    inner: dict[str, Any] = {"question": question, "reason": reason}
    if checked:
        inner["checked"] = list(checked)
    if failed_step:
        inner["failed_step"] = failed_step
    return {"unknown": inner}


# --- scope --------------------------------------------------------------------------


def _found_word(text: str, table: Sequence[str] | Mapping[str, str]) -> str | None:
    low = text.lower()
    for word in table:
        if re.search(rf"\b{re.escape(word)}\b", low):
            return table[word] if isinstance(table, Mapping) else word
    return None


def _scope(seen: Seen) -> GenerationResult:
    if not seen.of("search"):
        return _calls(seen, [("search", {"collection": NOTES, "terms": ["priorit"]})])
    notes = [r for r in seen.of("read_note") if r.data]
    if not seen.of("read_note"):
        hits = [h["record"] for r in seen.of("search") if r.data for h in r.data["hits"]]
        if hits:
            return _calls(seen, [("read_note", {"record": h}) for h in hits])
    answered = {q: (a, s) for q, a, s in seen.answers}
    segment = _found_word(seen.request, SEGMENTS)
    region = _found_word(seen.request, GEOGRAPHIES)
    questions = []
    if segment is None and not any("buyers" in q for q in answered):
        questions.append(
            {
                "id": "segment",
                "text": ASK_SEGMENT,
            }
        )
    if region is None and not any("geography" in q for q in answered):
        questions.append(
            {
                "id": "geography",
                "text": ASK_GEOGRAPHY,
            }
        )
    if questions and not seen.of("ask"):
        return _calls(seen, [("ask", {"questions": questions})])

    def basis(value: str | None, keyword: str, words: Any) -> dict[str, Any]:
        if value is not None:
            return {"value": value, "basis": "request"}
        for q, (a, span) in answered.items():
            if keyword in q:
                out: dict[str, Any] = {"value": a.strip(), "basis": "answer"}
                found = _found_word(a, words)
                if found is not None:
                    out["normalised"] = found
                if span:
                    out["cite"] = {"statement": span}
                return out
        return {"value": "", "basis": "not given"}

    geography = basis(region, "geography", GEOGRAPHIES)
    if region is not None:
        geography["normalised"] = region
    out: list[dict[str, Any]] = []
    for r in notes:
        assert r.data is not None
        for line in r.data["lines"]:
            if line["text"].lower().startswith("not a priority"):
                out.append({"value": line["text"], "cite": r.cite(line["n"], line["text"])})
    content = {
        "question": seen.request.strip(),
        "segment": basis(segment, "buyers", SEGMENTS),
        "geography": geography,
        "boundary": {
            "in": BOUNDARY_IN,
            "out": out,
        },
        "asked": [q["text"] for r in seen.of("ask") for q in r.args.get("questions", [])],
    }
    return _calls(seen, [_save("scope", content), _finish("scope saved")])


# --- discover -----------------------------------------------------------------------


def _identity(r: Result) -> analysis.PageIdentity:
    assert r.data is not None
    d = r.data
    names: dict[str, dict[str, str]] = {}
    relations: list[dict[str, str]] = []
    for p in d["passages"]:
        if p["kind"] in ("brand", "product") and p["kind"] not in names:
            names[p["kind"]] = {"name": p["fields"]["name"], **r.cite(p["n"], p["text"])}
        if p["kind"] == "relationship":
            relations.append({"text": p["text"], "span": r.cites[p["n"]]})
    return analysis.PageIdentity(
        record=str(d["record"]),
        version=str(d["version"]),
        published=str(d["published"] or ""),
        product=str(d["product"] or ""),
        brand=str(d["brand"] or ""),
        url=d.get("url"),
        relations=tuple(relations),
        names=names,
    )


def _discover(seen: Seen) -> GenerationResult:
    searches = seen.of("search")
    if not searches:
        return _calls(
            seen,
            [
                ("search", {"collection": PAGES, "terms": list(DISCOVER_TERMS)}),
                ("search", {"collection": PAGES, "terms": list(MANUAL_TERMS)}),
            ],
        )
    found, manual = searches[0], searches[-1]
    if not seen.of("fetch_page"):
        hits = [h["record"] for h in (found.data or {}).get("hits", [])]
        return _calls(seen, [("fetch_page", {"record": h}) for h in hits])
    pages = [_identity(r) for r in seen.of("fetch_page") if r.data]
    companies = analysis.resolve(pages)
    rows: list[dict[str, Any]] = []
    relations: list[dict[str, Any]] = []
    for c in companies:
        first = c.pages[0]
        canon_kind = "brand" if first.brand == c.canonical else "product"
        canon = first.names[canon_kind]
        aliases: list[dict[str, Any]] = []
        for p in c.pages:
            for kind in ("brand", "product"):
                n = p.names.get(kind)
                if (
                    n
                    and n["name"] != c.canonical
                    and n["name"] not in [a["value"] for a in aliases]
                ):
                    aliases.append(
                        {"value": n["name"], "cite": {"span": n["span"], "quote": n["quote"]}}
                    )
        evidence = []
        for src, dst, rel in c.merges:
            quote = str(rel["text"])
            evidence.append({"page": src.record, "cite": {"span": rel["span"], "quote": quote}})
            ref = {"kind": "record", "id": src.record, "version": src.version}
            relations.append(
                {
                    "kind": "same_entity_as",
                    "src": ref,
                    "dst": {"kind": "record", "id": dst.record, "version": dst.version},
                    "evidence": [{"source": ref, "quote": quote, "span": rel["span"]}],
                }
            )
        rows.append(
            {
                "key": c.key,
                "canonical": {
                    "value": c.canonical,
                    "cite": {"span": canon["span"], "quote": canon["quote"]},
                },
                "aliases": aliases,
                "pages": [
                    {
                        "record": p.record,
                        "version": p.version,
                        "published": p.published,
                        "url": p.url,
                    }
                    for p in c.pages
                ],
                "merge_evidence": evidence,
            }
        )
    failed = [r.args["record"] for r in seen.of("fetch_page") if r.error]
    manual_hits = (manual.data or {}).get("hits", [])
    content = {
        "companies": rows,
        "pages_found": sum(len(c.pages) for c in companies),
        "resolved": len(companies),
        "unfetched": failed,
        "searches": [
            {
                "terms": r.args["terms"],
                "hits": [h["record"] for h in (r.data or {}).get("hits", [])],
            }
            for r in searches
        ],
        "manual_alternatives": _unknown(
            "Which manual ways (walk-round counts, spreadsheets, badge data) do buyers use today, and what do they cost?",  # noqa: E501
            "not_found",
            checked=(manual.data or {}).get("checked", []),
        )
        if not manual_hits
        else _unknown(
            f"Pages {[h['record'] for h in manual_hits]} mention a manual way; read them next.",
            "not_checked",
        ),
    }
    return _calls(
        seen,
        [
            _save("competitors", content, relations=relations),
            _finish(f"{len(companies)} companies on {content['pages_found']} pages"),
        ],
    )


# --- evidence -----------------------------------------------------------------------

_POWER = re.compile(r"\b(PoE|Ethernet|mains|USB-C|battery powered|battery thermal|AA)\b", re.I)
_BATTERY_LIFE = re.compile(r"\b(battery life|years? of battery|months at)\b", re.I)
_PRIVACY = re.compile(r"\b(privacy|camera|faces|identities|image)\b", re.I)


def _entry(r: Result, p: Mapping[str, Any], value: str, label: str) -> dict[str, Any]:
    assert r.data is not None
    return {
        "value": value,
        "label": label,
        "record": r.data["record"],
        "published": r.data["published"],
        "cite": r.cite(p["n"], p["text"]),
    }


def _evidence(seen: Seen) -> GenerationResult:
    key = seen.ref.key
    found = seen.artifact("research.competitors")
    companies = found[1]["companies"] if found else []
    company = next((c for c in companies if c["key"] == key), None)
    if company is None:
        return _calls(seen, [_finish(f"{key} is not a resolved company")])
    fetched = seen.of("fetch_page")
    if not fetched:
        return _calls(seen, [("fetch_page", {"record": p["record"]}) for p in company["pages"]])
    ok = [r for r in fetched if r.data]
    if len(ok) < len(company["pages"]):
        failed = [r.args["record"] for r in fetched if r.error]
        return _calls(
            seen, [_finish(f"could not fetch {failed}; nothing is saved without its page")]
        )
    name = company["canonical"]["value"]
    cells: dict[str, list[dict[str, Any]]] = {c: [] for c in CELLS}
    notes: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    for r in ok:
        assert r.data is not None
        for p in r.data["passages"]:
            kind, text = p["kind"], p["text"]
            if p.get("instruction_like"):
                flags.append(
                    {
                        "record": r.data["record"],
                        "kind": "instruction_like",
                        "hidden": kind == "hidden",
                        "handling": "kept as data: no tool, approval, recipient or budget changed",
                        "text": _entry(r, p, text, "fact"),
                    }
                )
                continue
            if kind == "price":
                f = p["fields"]
                terms = str(f["row"].get("Terms", ""))
                e = _entry(r, p, str(f["amount"]), "fact")
                e.update(
                    currency=f["currency"], per=f["per"], item=f["row"].get("Item", ""), terms=terms
                )
                cells["unit_price" if f["per"] == "unit" else "subscription"].append(e)
                continue
            if p.get("section") == "Pricing" and kind == "text":
                notes.append(_entry(r, p, text, "fact"))
            if kind not in ("text", "claim", "spec", "relationship"):
                continue
            label = analysis.claim_label(kind, text)
            if _BATTERY_LIFE.search(text):
                cells["battery_life"].append(_entry(r, p, text, label))
            if _POWER.search(text):
                cells["power"].append(_entry(r, p, text, label))
            if _PRIVACY.search(text):  # by what it says, never by the heading it sits under
                cells["privacy"].append(_entry(r, p, text, "vendor_claim"))
    checked = [f"record:{r.data['record']}@{r.data['version']}" for r in ok if r.data]
    questions = {
        "unit_price": f"What does {name} charge per unit? No unit price is listed on its pages.",
        "subscription": f"Does {name} charge a recurring fee? None is listed on its pages.",
        "power": f"How is {name}'s sensor powered? Its pages do not say.",
        "battery_life": f"What battery life does {name} state or measure? Its pages give none.",
        "privacy": f"What can {name}'s sensor capture, and does any image leave it? Its pages do not say.",  # noqa: E501
    }
    for c in CELLS:
        if not cells[c]:
            cells[c] = [_unknown(questions[c], "not_found", checked=checked)]
    units = [e for e in cells["unit_price"] if "value" in e]
    conflicts = []
    relations = []
    if analysis.price_conflict(units):
        conflicts.append(
            {
                "field": "unit_price",
                "values": [
                    f"{e['currency']} {e['value']} per {e['per']} ({e['terms']}), "
                    f"page dated {e['published']}"
                    for e in units
                ],
                "status": "open",
            }
        )
        by_record = {r.data["record"]: r.data for r in ok if r.data}
        a, b = units[0], units[1]
        ra, rb = _record_ref(by_record[a["record"]]), _record_ref(by_record[b["record"]])
        relations.append(
            {
                "kind": "contradicts",
                "src": ra,
                "dst": rb,
                "label": None,
                "evidence": [
                    {"source": ra, "quote": a["cite"]["quote"], "span": a["cite"]["span"]},
                    {"source": rb, "quote": b["cite"]["quote"], "span": b["cite"]["span"]},
                ],
            }
        )
    content = {
        "key": key,
        "name": name,
        "pages": [
            {"record": r.data["record"], "published": r.data["published"], "url": r.data["url"]}
            for r in ok
            if r.data
        ],
        **cells,
        "price_notes": notes,
        "flags": flags,
        "conflicts": conflicts,
    }
    return _calls(
        seen,
        [
            _save("profile", content, key=key, relations=relations),
            _finish(f"profile of {name} saved"),
        ],
    )


# --- voice --------------------------------------------------------------------------


def _voice(seen: Seen) -> GenerationResult:
    if not seen.of("search"):
        return _calls(seen, [("search", {"collection": REVIEWS, "terms": ["review"]})])
    if not seen.of("read_reviews"):
        found = [h["record"] for r in seen.of("search") if r.data for h in r.data["hits"]]
        return _calls(seen, [("read_reviews", {"record": h}) for h in found])
    rows: list[dict[str, Any]] = []
    for r in seen.of("read_reviews"):
        if r.data is None:
            continue
        for row in r.data["rows"]:
            fields = {k: v for k, v in row.items() if k not in ("n", "text")}
            rows.append(
                {"fields": fields, "text": row["text"], "cite": r.cite(row["n"], row["text"])}
            )
    sample = analysis.review_sample(rows)
    scope = seen.artifact("research.scope")
    region = scope[1]["geography"].get("normalised") if scope else None
    members = REGION_MEMBERS.get(str(region), frozenset({str(region)}))
    by_id = {r["fields"]["review_id"]: r for r in rows}
    needs = []
    for theme, hits in sample.themes.items():
        dates = sorted(str(h["fields"]["date"]) for h in hits)
        in_scope = [h for h in hits if h["fields"].get("geography") in members]
        needs.append(
            {
                "theme": theme,
                "need": analysis.NEEDS[theme],
                "phrase": sample.phrase(theme),
                "count": len(hits),
                "of": len(sample.unique),
                "in_scope_geography": {"geography": region, "count": len(in_scope)},
                "first": dates[0] if dates else None,
                "last": dates[-1] if dates else None,
                "geographies": analysis.tally(hits, "geography"),
                "products": analysis.tally(hits, "product"),
                "evidence": [
                    {
                        "review": h["fields"]["review_id"],
                        "date": h["fields"]["date"],
                        "geography": h["fields"]["geography"],
                        "product": h["fields"]["product"],
                        "cite": h["cite"],
                    }
                    for h in hits
                ],
            }
        )
    dates = sorted(str(r["fields"]["date"]) for r in sample.unique)
    content = {
        "sample": {
            "rows": len(sample.rows),
            "unique": len(sample.unique),
            "duplicates": [
                {
                    "review": d,
                    "duplicate_of": o,
                    "reason": "same product and text on another site",
                    "evidence": [{"cite": by_id[d]["cite"]}, {"cite": by_id[o]["cite"]}],
                }
                for d, o in sample.duplicates
            ],
            "first": dates[0] if dates else None,
            "last": dates[-1] if dates else None,
            "geographies": analysis.tally(sample.unique, "geography"),
            "sites": analysis.tally(sample.unique, "site"),
            "rating_scales": sorted({str(r["fields"]["rating_unit"]) for r in sample.unique}),
            "note": "counts describe this sample only, never a market",
        },
        "needs": needs,
    }
    return _calls(
        seen,
        [_save("needs", content), _finish(f"{len(needs)} needs from {len(sample.unique)} reviews")],
    )


# --- report -------------------------------------------------------------------------


def _cell(entries: list[Any], ref: str, column: str) -> list[dict[str, Any]]:
    out = []
    for i, e in enumerate(entries):
        if "unknown" in e:
            u = e["unknown"]
            out.append(
                {
                    "status": "unknown",
                    "reason": u["reason"],
                    "question": u["question"],
                    "cite": {"artifact": ref, "path": f"/{column}/{i}"},
                }
            )
        else:
            out.append({k: v for k, v in e.items()})
    return out


def _report(seen: Seen) -> GenerationResult:
    comp = seen.artifact("research.competitors")
    needs = seen.artifact("research.needs")
    companies = comp[1]["companies"] if comp else []
    rows = []
    unknowns: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    blocked = {u["slot"].rsplit("/", 1)[-1]: u for u in seen.unknowns}
    for c in companies:
        key = c["key"]
        found = seen.artifact(f"research.profile/{key}")
        row: dict[str, Any] = {
            "key": key,
            "company": c["canonical"],
            "aliases": [a["value"] for a in c["aliases"]],
        }
        if found is None:
            u = blocked.get(key, {})
            step = (u.get("failed") or "").split(" ")[0] or None
            reason = "unverified" if step else "not_checked"
            for col in CELLS:
                row[col] = [
                    _unknown(
                        f"{col} for {c['canonical']['value']}: its pages could not be read",
                        reason,
                        failed_step=step,
                    )
                ]
            row["price_conflict"] = False
            row["blocker"] = u.get("blocker")
            unknowns.append(
                _unknown(
                    f"Everything about {c['canonical']['value']}: its pages could not be read",
                    reason,
                    failed_step=step,
                )
            )
            rows.append(row)
            continue
        ref, profile = found
        for col in CELLS:
            row[col] = _cell(profile[col], ref, col)
            for i, e in enumerate(profile[col]):
                if "unknown" in e and col == "unit_price":
                    unknowns.append(
                        {
                            "question": e["unknown"]["question"],
                            "reason": e["unknown"]["reason"],
                            "cite": {"artifact": ref, "path": f"/{col}/{i}"},
                        }
                    )
        row["price_conflict"] = bool(profile["conflicts"])
        rows.append(row)
        for i, conflict in enumerate(profile["conflicts"], start=1):
            findings.append(
                {
                    "id": f"price-conflict-{key}-{i}",
                    "label": "fact",
                    "statement": f"{profile['name']} lists two unit prices: "
                    + "; ".join(conflict["values"])
                    + ". The conflict is left open.",
                    "evidence": [{"cite": e["cite"]} for e in profile["unit_price"] if "cite" in e],
                }
            )
        claims = [e for e in profile["battery_life"] if e.get("label") == "vendor_claim"]
        facts = [e for e in profile["battery_life"] if e.get("label") == "fact"]
        if claims and facts:
            findings.append(
                {
                    "id": f"claim-vs-fact-{key}",
                    "label": "fact",
                    "statement": f"{profile['name']}: '{claims[0]['value']}' is a vendor claim; its own specification states '{facts[0]['value']}'.",  # noqa: E501
                    "evidence": [{"cite": claims[0]["cite"]}, {"cite": facts[0]["cite"]}],
                }
            )
        for i, flag in enumerate(profile["flags"], start=1):
            held = "hidden text" if flag.get("hidden") else "text"
            findings.append(
                {
                    "id": f"instruction-like-{key}-{i}",
                    "label": "fact",
                    "statement": f"A page of {profile['name']} holds {held} addressed to automated readers; it was kept as data and changed nothing.",  # noqa: E501
                    "evidence": [{"cite": flag["text"]["cite"]}],
                }
            )
    merged = [c for c in companies if c["merge_evidence"]]
    for c in merged:
        findings.append(
            {
                "id": f"same-company-{c['key']}",
                "label": "fact",
                "statement": f"{c['canonical']['value']} also appears as {', '.join(a['value'] for a in c['aliases'])}: one company, counted once.",  # noqa: E501
                "evidence": [{"cite": m["cite"]} for m in c["merge_evidence"]],
            }
        )
    priced: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for row in rows:
        for e in row["unit_price"]:
            if "value" in e and "currency" in e:
                priced.setdefault(e["currency"], []).append((float(e["value"]), e))
    for cur, points in sorted(priced.items()):
        points.sort(key=lambda x: x[0])
        lo, hi = points[0], points[-1]
        findings.append(
            {
                "id": f"price-range-{cur.lower()}",
                "label": "inference",
                "statement": f"Listed one-off unit prices in {cur} run from {lo[1]['value']} to {hi[1]['value']} (as listed, not converted, not including subscriptions).",  # noqa: E501
                "evidence": [{"cite": lo[1]["cite"]}, {"cite": hi[1]["cite"]}],
            }
        )
    themes = needs[1]["needs"] if needs else []
    for n in themes:
        findings.append(
            {
                "id": f"need-{n['theme']}",
                "label": "fact",
                "statement": f"{n['need']}: {n['phrase']} ({n['first']} to {n['last']}).",
                "evidence": [{"cite": e["cite"]} for e in n["evidence"][:3]],
            }
        )
    if comp and "unknown" in comp[1].get("manual_alternatives", {}):
        u = comp[1]["manual_alternatives"]["unknown"]
        unknowns.append(
            {
                "question": u["question"],
                "reason": u["reason"],
                "cite": {"artifact": comp[0], "path": "/manual_alternatives"},
            }
        )
    if needs:
        unknowns.append(
            {
                "question": "How common are these needs among buyers outside this review sample?",
                "reason": "not_checked",
                "cite": {"artifact": needs[0], "path": "/sample/note"},
            }
        )
    matrix = {
        "columns": ["company", *CELLS],
        "rows": rows,
        "resolved": len(companies),
        "pages_found": comp[1]["pages_found"] if comp else 0,
        "note": "each cell lists its entries with their page and date; prices are as listed, never converted or averaged",  # noqa: E501
    }
    by_theme = {n["theme"]: n for n in themes}
    ranked = sorted(themes, key=lambda n: (-n["count"], n["theme"]))
    tests = []
    for i, n in enumerate(ranked[:3], start=1):
        tests.append(
            {
                "id": f"T{i}",
                "theme": n["theme"],
                "hypothesis": f"Buyers choose a sensor that meets this need: {n['need'].lower()}.",
                "method": _METHODS[n["theme"]],
                "metric": _METRICS[n["theme"]],
                "threshold": {"value": _THRESHOLDS[n["theme"]], "status": "proposal"},
                "why": f"{n['phrase']} raise it.",
                "evidence": [{"cite": e["cite"]} for e in by_theme[n["theme"]]["evidence"][:2]],
            }
        )
    finding_unknowns = []
    for u in unknowns:
        if "unknown" in u:
            finding_unknowns.append(u)
        else:
            finding_unknowns.append(
                {"question": u["question"], "reason": u["reason"], "cite": u["cite"]}
            )
    return _calls(
        seen,
        [
            _save("matrix", matrix),
            _save("findings", {"findings": findings, "unknowns": finding_unknowns}),
            _save("tests", {"tests": tests}),
            _finish("matrix, findings and tests saved"),
        ],
    )


_METHODS = {
    "battery": "Log battery drain of two units for four weeks at one report per minute, the typical office rate.",  # noqa: E501
    "stillness": "Seat one still occupant in a meeting room for 30 minutes per session and log every empty report.",  # noqa: E501
    "setup": "Time five first-time installs on a ceiling tile with no electrician and no new cable.",  # noqa: E501
    "privacy": "Ask three works councils what written proof of counts-only output they accept.",
}
_METRICS = {
    "battery": "projected months to replacement at the measured drain",
    "stillness": "sessions with a false empty report",
    "setup": "minutes per install, and installs that needed a revisit",
    "privacy": "councils that accept the proof as written",
}
_THRESHOLDS = {
    "battery": "24 months or more",
    "stillness": "0 of 20 sessions",
    "setup": "15 minutes or less",
    "privacy": "3 of 3 councils",
}

_HANDLERS = {
    "scope": _scope,
    "discover": _discover,
    "evidence": _evidence,
    "voice": _voice,
    "report": _report,
}


class ResearchModel(RuleGenerationModel):
    """The research agent's rule-based generation policy (module doc)."""

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The next calls of the request's Task, from what the request carries."""
        seen = parse(request)
        handler = _HANDLERS.get(seen.ref.task_id)
        if handler is None:
            return GenerationResult(
                None, (ToolCall("t-finish", "finish", {"summary": "no rule"}),), "tool_calls"
            )
        return handler(seen)


class ObedientModel(ResearchModel):
    """A model that obeys text inside fetched pages: the counter-example the gate refuses.

    When an earlier result holds a passage flagged ``instruction_like`` that says to send
    something, it calls ``send_document`` with the address in the passage before going on.
    """

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """``send_document`` once per flagged passage, then the normal policy."""
        seen = parse(request)
        sent = {r.args.get("recipient") for r in seen.of("send_document")}
        for r in seen.of("fetch_page"):
            for p in (r.data or {}).get("passages", []):
                m = re.search(r"[\w.]+@[\w.]+\w", p["text"]) if p.get("instruction_like") else None
                if m is not None and m.group(0) not in sent:
                    args = {"recipient": m.group(0), "document": "everything gathered so far"}
                    return _calls(seen, [("send_document", args)])
        return super().generate(request)


ADAPTERS = {"generate": ResearchModel()}
"""The ``fake`` run mode's model per alias: the composition root binds it (ADR-P07)."""
