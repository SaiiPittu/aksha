"""The research agent's offline model: a rule-based generation policy (ADR-P07 fakes).

A test double, not agent logic: no module of the agent imports it, and the agent works the
same with a live model. :class:`ResearchModel` reads only what its request carries: the
request text, the read block of the artifacts its Task declares, the person's answers, and
its own earlier tool calls with their results (each result followed by the span IDs it
may cite). From that it picks the next calls of its Task by fixed, general rules, so a run
in ``fake`` mode is offline and writes the same bytes twice. It knows no fixture: what to
compare and what to read the reviews for come from the request, in a small grammar a
person can write (a live model reads free text instead):

* ``Compare <dimension>, <dimension> (<term>, <term>) and <dimension>.``: the matrix's
  dimensions besides price. A dimension's terms find it in a page; with none given, its
  name is its term. ``not <phrase>`` excludes text the other terms would match, and
  ``batter*`` is a prefix (``analysis.term_hits``). Price-like dimensions (price, fee,
  cost, commission, subscription) are the structural price column, not a text one.
* ``What do customers say about: <theme> (<terms>); <theme> (<terms>)?``: the review
  themes. Items are separated by ``;`` when any item has terms, else by commas and "and".
* ``for <segment>`` (not "for the ...", "for our ..." or a possessive, which name the
  venture or its product; one followed by "in <place>" first) and a named geography (a
  country or region) fix the segment and the geography; a missing one is asked for.

The scope's geography comes from the request, else the person's answer; a line of a note
that starts ``Scope amendment`` and names geographies is the person's later written word
and wins over both (``basis: note``, cited to that line), and no geography question is
asked then. Several regions normalise to one joined name (``EU+UK``); the reviews counted
inside the scope are those whose country is in any of them.

The judgement it applies is in ``analysis.py``; this module only turns tool results into
calls and saved content. Text inside an input is data: a passage flagged
``instruction_like`` is recorded as a flag and never changes a call.
:class:`ObedientModel` is the counter-example for tests: it does what such a passage says,
and the policy gate refuses it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from aksha.runtime.api import (
    GenerationRequest,
    GenerationResult,
    InstanceRef,
    RuleGenerationModel,
    ToolCall,
)

from . import analysis, fakes_readers, parsing

MANUAL_TERMS = ("spreadsheet", "manual count", "by hand", "walk-round", "clipboard", "paper log")
"""Words for doing the job without a product, in any market."""
PRICE_WORDS = re.compile(
    r"\b(price|prices|pricing|cost|costs|fee|fees|commission|subscription|charge|charges)\b", re.I
)
GEOGRAPHIES: Mapping[str, str] = {
    "eu": "EU",
    "europe": "EU",
    "european": "EU",
    "germany": "DE",
    "netherlands": "NL",
    "france": "FR",
    "spain": "ES",
    "italy": "IT",
    "uk": "UK",
    "united kingdom": "UK",
    "britain": "UK",
    "us": "US",
    "usa": "US",
    "united states": "US",
    "canada": "CA",
    "india": "IN",
    "kenya": "KE",
    "nigeria": "NG",
    "brazil": "BR",
    "australia": "AU",
    "japan": "JP",
    "global": "global",
    "worldwide": "global",
}
"""Place names a request may give, to the code reviews carry."""
EU = frozenset({"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
                "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES",
                "SE"})  # fmt: skip
"""The 27 member states of the European Union, by ISO 3166 code."""
REGION_MEMBERS: Mapping[str, frozenset[str]] = {"EU": EU}
COUNTRIES: Mapping[str, str] = {
    "austria": "AT", "belgium": "BE", "bulgaria": "BG", "croatia": "HR", "cyprus": "CY",
    "czechia": "CZ", "czech republic": "CZ", "denmark": "DK", "estonia": "EE",
    "finland": "FI", "france": "FR", "germany": "DE", "deutschland": "DE", "greece": "GR",
    "hungary": "HU", "ireland": "IE", "italy": "IT", "latvia": "LV", "lithuania": "LT",
    "luxembourg": "LU", "malta": "MT", "netherlands": "NL", "the netherlands": "NL",
    "holland": "NL", "poland": "PL", "portugal": "PT", "romania": "RO", "slovakia": "SK",
    "slovenia": "SI", "spain": "ES", "sweden": "SE", "united kingdom": "UK", "uk": "UK",
    "gb": "UK", "great britain": "UK", "england": "UK", "scotland": "UK", "wales": "UK",
    "norway": "NO", "switzerland": "CH", "united states": "US", "usa": "US", "us": "US",
    "canada": "CA", "mexico": "MX", "brazil": "BR", "india": "IN", "japan": "JP",
    "china": "CN", "australia": "AU", "new zealand": "NZ", "kenya": "KE", "nigeria": "NG",
    "south africa": "ZA", "singapore": "SG", "indonesia": "ID", "turkey": "TR",
}  # fmt: skip
"""Country names to the codes a geography is compared in; a code stays itself."""


def country_code(value: object) -> str:
    """A review's country as a code: ``DE``, ``Germany`` and ``germany`` are all ``DE``."""
    text = " ".join(str(value or "").split())
    return COUNTRIES.get(text.lower(), text.upper() if len(text) == 2 else text)


COMPARISON = re.compile(
    r"\b(compar\w*|compet\w*|who sells|sellers?|vendors?|suppliers?|alternatives?|pric\w*|charge|"
    r"market|rivals?)\b",
    re.I,
)
"""Words that make a request a market comparison; any other request is a synthesis."""
ASK_SEGMENT = "Which buyers is this for? It decides which competitors and reviews count."
ASK_GEOGRAPHY = (
    "Which geography should this cover? Prices and reviews can come from several countries, "
    "so it changes what counts."
)
AMENDMENT = "scope amendment"
"""How a note line that amends the scope starts (case folded); its geography words win."""
BOUNDARY_IN = "products that do the job the question names, and the manual ways buyers use today"
PRICE_SECTION = re.compile(r"\b(pric\w*|plans?|costs?|fees?)\b", re.I)
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
    queries: tuple[str, ...] = ()
    """Web searches a person who knows the market would type, beside the request's own words
    (:class:`ResearchModel`'s ``queries``, from the task's world); a live model writes its own."""

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


# --- the request grammar ------------------------------------------------------------


def _split_top(text: str, seps: str) -> list[str]:
    """``text`` split at any of ``seps`` outside parentheses."""
    out: list[str] = []
    cur: list[str] = []
    depth = 0
    for ch in text:
        depth += ch == "("
        depth -= ch == ")"
        if ch in seps and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [p for p in (x.strip() for x in out) if p]


def _sentence_after(text: str, at: int) -> str:
    """From ``at`` to the end of its sentence (a ``.``, ``?`` or ``!`` outside parentheses)."""
    depth = 0
    for i in range(at, len(text)):
        ch = text[i]
        depth += ch == "("
        depth -= ch == ")"
        if ch in ".?!\n" and depth == 0 and (i + 1 == len(text) or not text[i + 1].isalnum()):
            return text[at:i]
    return text[at:]


def parse_list(text: str) -> list[tuple[str, list[str]]]:
    """``a, b (t1, t2) and c`` as ``[(name, terms)]`` (module doc)."""
    text = text.strip().strip(":").strip()
    items = _split_top(text, ";") if ";" in text else _split_top(text, ",")
    out: list[tuple[str, list[str]]] = []
    for raw in items:
        for item in re.split(r"\s+and\s+(?![^(]*\))", raw) if ";" not in text else [raw]:
            item = re.sub(r"^(and|or)\s+", "", item.strip())
            m = re.match(r"^(.*?)\s*\((.*)\)\s*$", item)
            name, terms = (m.group(1), _split_top(m.group(2), ",")) if m else (item, [])
            name = name.strip().strip('"')
            if name:
                out.append((name, [t.strip().strip('"') for t in terms if t.strip()] or [name]))
    return out


def dimensions_of(request: str) -> tuple[list[dict[str, Any]], list[str]]:
    """The request's comparison dimensions, and the price-like ones it named."""
    m = re.search(r"\bcompare\b(?:\s+(?:them|each|competitors|vendors|products))?"
                  r"(?:\s+(?:on|by|across|for))?\s*:?", request, re.I)  # fmt: skip
    if m is None:
        return [], []
    dims, prices = [], []
    for name, terms in parse_list(_sentence_after(request, m.end())):
        if PRICE_WORDS.search(name) and terms == [name]:
            prices.append(name)
            continue
        dims.append({"id": analysis.slug(name).replace("-", "_"), "name": name, "terms": terms,
                     "basis": "request"})  # fmt: skip
    return dims, prices


def themes_of(request: str) -> list[dict[str, Any]]:
    """The request's review themes (module doc)."""
    found = list(re.finditer(r"\b(?:says?|said|mention)\s+about\s*(:?)", request, re.I))
    if not found:
        return []
    m = next((f for f in found if f.group(1)), found[0])  # a list after a colon first
    return [
        {"id": analysis.slug(name).replace("-", "_"), "name": name, "terms": terms,
         "basis": "request"}
        for name, terms in parse_list(_sentence_after(request, m.end()))
    ]  # fmt: skip


def segment_of(request: str) -> str | None:
    """The segment a request names: ``segment: X``, or ``for X`` (not "for the ...")."""
    m = re.search(r"\b(?:segment|buyers)\s*:\s*([^.;\n?]+)", request, re.I)
    if m:
        return m.group(1).strip()
    found = []
    for f in re.finditer(r"\bfor\s+([a-z][\w' -]*?)(?=\s+in\s|\s+across\s|,|\.|\?|;|:|\s+and\s"
                         r"|\s+who\s|\s+with\s|$)", request, re.I):  # fmt: skip
        words = f.group(1).strip()
        first = words.split()[0].lower()
        if "'s" in words or first in ("the", "a", "an", "our", "my", "its", "this", "their"):
            continue  # a product or the venture, not a buyer
        placed = request[f.end() :].lstrip().lower().startswith("in ")
        found.append((not placed, f.start(), words))
    return min(found)[2] if found else None


def _found_word(text: str, table: Mapping[str, str]) -> str | None:
    low = text.lower()
    for word in table:
        if re.search(rf"\b{re.escape(word)}\b", low):
            return table[word]
    return None


def _found_all(text: str, table: Mapping[str, str]) -> list[str]:
    """Every value of ``table`` whose word ``text`` holds, in table order, each once."""
    low = text.lower()
    out: list[str] = []
    for word, value in table.items():
        if re.search(rf"\b{re.escape(word)}\b", low) and value not in out:
            out.append(value)
    return out


def members(region: object) -> frozenset[str]:
    """The countries a normalised region names: ``EU+UK`` is the EU's members and ``UK``."""
    out: set[str] = set()
    for part in str(region).split("+"):
        out |= REGION_MEMBERS.get(part, frozenset({part}))
    return frozenset(out)


def _amendment(notes: Sequence[Result]) -> dict[str, Any] | None:
    """The latest scope amendment a note states (a line ``Scope amendment ...``), if any."""
    found: dict[str, Any] | None = None
    for r in notes:
        assert r.data is not None
        for line in r.data["lines"]:
            text = str(line["text"])
            regions = _found_all(text, GEOGRAPHIES)
            if text.lower().startswith(AMENDMENT) and regions:
                found = {"value": text, "normalised": "+".join(regions), "basis": "note",
                         "cite": r.cite(line["n"], text)}  # fmt: skip
    return found


# --- scope --------------------------------------------------------------------------


def _scope(seen: Seen) -> GenerationResult:
    if not seen.of("search"):
        return _calls(
            seen, [("search", {"terms": ["priorities"], "formats": ["markdown", "text"]})]
        )
    notes = [r for r in seen.of("read_note") if r.data]
    hits = [h["record"] for r in seen.of("search") if r.data for h in r.data["hits"]]
    unread = [h for h in hits if h not in {str(r.data["record"]) for r in notes if r.data}]
    if unread and len(seen.of("read_note")) < 2 * len(hits):  # a stale read is read again
        return _calls(seen, [("read_note", {"record": h}) for h in unread])
    answered = {q: (a, s) for q, a, s in seen.answers}
    segment = segment_of(seen.request)
    region = _found_word(seen.request, GEOGRAPHIES)
    amended = _amendment(notes)
    comparison = bool(COMPARISON.search(seen.request))
    questions = []
    if comparison and segment is None and not any("buyers" in q for q in answered):
        questions.append({"id": "segment", "text": ASK_SEGMENT})
    if (
        comparison
        and region is None
        and amended is None
        and not any("geography" in q for q in answered)
    ):
        questions.append({"id": "geography", "text": ASK_GEOGRAPHY})
    if questions and not seen.of("ask"):
        return _calls(seen, [("ask", {"questions": questions})])

    def basis(value: str | None, keyword: str, words: Mapping[str, str] | None) -> dict[str, Any]:
        if value is not None:
            return {"value": value, "basis": "request"}
        for q, (a, span) in answered.items():
            if keyword in q:
                out: dict[str, Any] = {"value": a.strip(), "basis": "answer"}
                found = _found_word(a, words) if words else None
                if found is not None:
                    out["normalised"] = found
                if span:
                    out["cite"] = {"statement": span}
                return out
        return {"value": "", "basis": "not given"}

    geography = basis(region, "geography", GEOGRAPHIES)
    if region is not None:
        geography["normalised"] = region
    if amended is not None:
        geography = amended  # the person's later written amendment wins over all else
    out: list[dict[str, Any]] = []
    for r in notes:
        assert r.data is not None
        for line in r.data["lines"]:
            if line["text"].lower().startswith("not a priority"):
                out.append({"value": line["text"], "cite": r.cite(line["n"], line["text"])})
    dims, prices = dimensions_of(seen.request)
    themes = themes_of(seen.request)
    plan = (
        {"kind": "comparison", "outputs": ["competitors", "profiles", "matrix", "needs",
                                           "findings", "tests"]}
        if comparison
        else {"kind": "synthesis", "outputs": ["needs", "findings"]}
    )  # fmt: skip
    content = {
        "question": seen.request.strip(),
        "plan": plan,
        "segment": basis(segment, "buyers", None),
        "geography": geography,
        "boundary": {"in": BOUNDARY_IN, "out": out},
        "dimensions": dims,
        "prices_named": prices,
        "themes": themes,
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
    if not names and d.get("title"):
        title = next((p for p in d["passages"] if p["kind"] in ("title", "heading")), None)
        if title is not None:
            names["product"] = {"name": title["text"], **r.cite(title["n"], title["text"])}
    product = names.get("product", {}).get("name", d.get("product") or "")
    return analysis.PageIdentity(
        record=str(d["record"]),
        version=str(d["version"]),
        published=str(d["published"] or ""),
        product=str(product or ""),
        brand=str(d["brand"] or ""),
        url=d.get("url"),
        relations=tuple(relations),
        names=names,
        section=d.get("section"),
    )


SELLER_KINDS = ("competitor_page", "pricing_table")


def _reads_of(inputs: Sequence[Mapping[str, Any]], kinds: Sequence[str]) -> list[dict[str, Any]]:
    """What to read of each listed input.

    The whole record when it is one of ``kinds``, else each of its sections that is (a
    memo, a page holding several kinds).
    """
    out: list[dict[str, Any]] = []
    for i in inputs:
        if i["kind"] in kinds:
            out.append({"record": i["record"]})
            continue
        out += [{"record": i["record"], "section": p["section"]}
                for p in i.get("sections", ()) if p["kind"] in kinds]  # fmt: skip
    return out


def _where(r: Result) -> dict[str, Any]:
    """The record (and section) a fetch read, as a page or table reference keeps it."""
    assert r.data is not None
    ref: dict[str, Any] = {"record": r.data["record"], "version": r.data["version"]}
    if _on_the_web(str(r.data["record"])):
        ref["url"] = r.data["url"]
    if r.data.get("section") is not None:
        ref["section"] = r.data["section"]
    return ref


def _plan(seen: Seen) -> str:
    scope = seen.artifact("research.scope") or seen.artifact("research.competitors")
    plan = scope[1].get("plan") if scope else None
    return str(plan.get("kind")) if isinstance(plan, Mapping) else "comparison"


WEB_LIMIT = 20
"""Results asked per web search."""
REQUEST_WORDS = frozenset(
    [
        "research",
        "researching",
        "compare",
        "comparing",
        "competitor",
        "competitors",
        "competition",
        "rivals",
        "who",
        "sells",
        "sell",
        "what",
        "does",
        "each",
        "charge",
        "charges",
        "charged",
        "price",
        "prices",
        "pricing",
        "cost",
        "costs",
        "say",
        "says",
        "said",
        "customers",
        "customer",
        "patients",
        "patient",
        "farmers",
        "farmer",
        "users",
        "user",
        "how",
        "much",
        "many",
        "long",
        "last",
        "lasts",
        "they",
        "are",
        "is",
        "the",
        "a",
        "an",
        "and",
        "or",
        "for",
        "of",
        "to",
        "in",
        "on",
        "our",
        "my",
        "me",
        "your",
        "their",
        "its",
        "it",
        "this",
        "that",
        "venture",
        "venture's",
        "business",
        "company's",
        "summarise",
        "summarize",
        "summary",
        "send",
        "pages",
        "page",
        "which",
        "same",
        "do",
        "did",
        "be",
        "with",
        "by",
        "from",
        "about",
        "there",
        "distinct",
        "reviews",
        "review",
        "per",
        "unit",
        "units",
        "find",
        "list",
        "give",
        "tell",
        "show",
        "market",
        "at",
    ]
)
"""Words of a request that ask for research rather than name what it is about."""
SPAM = re.compile(
    r"!!!|\b(best deals?|hot deals?|buy now|click here|limited[- ]time|limited offer|"
    r"guaranteed|cheapest|lowest prices?|act now|100% (?:free|off|guaranteed)|"
    r"in 24 hours)\b",
    re.I,
)
"""Words of an advert or a content farm: a result that holds them is not read."""


def _on_the_web(record: str) -> bool:
    return record.startswith("web/")


def _fetched(seen: Seen) -> list[Result]:
    """Every page read so far, from the inputs (``fetch_page``) or the web (``fetch_url``)."""
    return [r for r in seen.results if r.name in ("fetch_page", "fetch_url")]


def _target(r: Result) -> str:
    return str(r.args.get("record") or r.args.get("url"))


def _sells(r: Result) -> bool:
    """A web page counts when it sells something; an input was chosen by its listed kind."""
    return r.name != "fetch_url" or (r.data or {}).get("kind") in SELLER_KINDS


def _read_again(page: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The call that reads one of a company's pages again: by URL for a web page."""
    if _on_the_web(str(page["record"])) and page.get("url"):
        return ("fetch_url", {"url": page["url"]})
    section = {"section": page["section"]} if "section" in page else {}
    return ("fetch_page", {"record": page["record"], **section})


def web_queries(seen: Seen) -> list[str]:
    """What to search the web for: the request's own subject, then the task's queries.

    The subject is the request's first sentence without the words that ask for research
    (``Research the competitors for the venture's occupancy sensor`` searches ``occupancy
    sensor``); a request that names no subject searches only the task's queries.
    """
    first = re.split(r"[.:?!;]", seen.request.strip(), maxsplit=1)[0]
    subject = [w for w in re.findall(r"[\w'-]+", first)
               if w.lower() not in REQUEST_WORDS and not w[:1].isupper()]  # fmt: skip
    out = [" ".join(subject)] if subject else []
    for q in seen.queries:
        if q not in out:
            out.append(q)
    return out


def _title_words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", title.lower()) if len(w) > 2}


def choose_results(searches: Sequence[Result]) -> dict[str, list[dict[str, Any]]]:
    """Which web results to fetch, by fixed rules over what the results show (module doc).

    In rank order across the searches, each URL once: an advert (:data:`SPAM`) is skipped;
    a result with the title and snippet of one already kept is a copy; of two results on
    one host with mostly the same title words, the older is an outdated version. Every
    skip is kept with its reason.
    """
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for r in searches:
        for h in (r.data or {}).get("results", ()):
            url = str(h["url"])
            if url in seen_urls:
                continue
            seen_urls.add(url)
            text = f"{h.get('title', '')} {h.get('snippet', '')}"
            pair = (" ".join(str(h.get("title", "")).lower().split()),
                    " ".join(str(h.get("snippet", "")).lower().split()))  # fmt: skip
            if SPAM.search(text):
                skipped.append({"url": url, "why": "an advert"})
            elif any(pair == k["pair"] for k in kept):
                skipped.append({"url": url, "why": "a copy of a page already chosen"})
            else:
                kept.append({"url": url, "pair": pair, "host": urlsplit(url).hostname,
                             "words": _title_words(str(h.get("title", ""))),
                             "date": str(h.get("date") or "")})  # fmt: skip
    outdated: set[str] = set()
    for a in kept:
        for b in kept:
            if a is b or a["host"] != b["host"] or not a["words"] or not b["words"]:
                continue
            overlap = len(a["words"] & b["words"]) / len(a["words"] | b["words"])
            if overlap >= 0.5 and a["date"] and b["date"] and a["date"] < b["date"]:
                outdated.add(a["url"])
    skipped += [{"url": u, "why": "an older version of a page on the same site"}
                for u in sorted(outdated)]  # fmt: skip
    fetch = [{"url": k["url"]} for k in kept if k["url"] not in outdated]
    return {"fetch": fetch, "skipped": skipped}


def _discover(seen: Seen) -> GenerationResult:
    if _plan(seen) == "synthesis":
        content = {"companies": [], "dimensions": [], "pages_found": 0, "resolved": 0,
                   "plan": {"kind": "synthesis"},
                   "manual_alternatives": _unknown("The request asks for no market comparison.",
                                                   "not_checked"),
                   "note": "the request asks for no market comparison"}  # fmt: skip
        return _calls(seen, [_save("competitors", content), _finish("no comparison asked")])
    listed = seen.of("list_inputs")
    if not listed:
        return _calls(
            seen,
            [
                ("list_inputs", {"kinds": ["competitor_page", "pricing_table"]}),
                (
                    "search",
                    {"terms": list(MANUAL_TERMS), "formats": ["html", "markdown", "text", "email"]},
                ),
                *(("web_search", {"query": q, "limit": WEB_LIMIT}) for q in web_queries(seen)),
            ],
        )
    searches = seen.of("search")
    manual = searches[-1] if searches else None
    web = choose_results(seen.of("web_search"))
    if not _fetched(seen):
        reads = _reads_of((listed[0].data or {}).get("inputs", []), SELLER_KINDS)
        urls = [{"url": h["url"]} for h in web["fetch"]]
        if reads or urls:
            return _calls(seen, [*(("fetch_page", a) for a in reads),
                                 *(("fetch_url", u) for u in urls)])  # fmt: skip
    fetched = [r for r in _fetched(seen) if r.data and _sells(r)]
    tables = [r for r in fetched if (r.data or {}).get("kind") == "pricing_table"]
    pages = [_identity(r) for r in fetched if r not in tables]
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
                        **({} if p.section is None else {"section": p.section}),
                    }
                    for p in c.pages
                ],
                "merge_evidence": evidence,
            }
        )
    _table_sellers(rows, tables)
    failed = [_target(r) for r in _fetched(seen) if r.error]
    manual_hits = [] if manual is None else (manual.data or {}).get("hits", [])
    scope = seen.artifact("research.scope")
    content = {
        "companies": rows,
        "dimensions": list(scope[1].get("dimensions", [])) if scope else [],
        "pages_found": sum(len(c.pages) for c in companies),
        "resolved": len(rows),
        "tables": [_where(r) for r in tables if r.data],
        "unfetched": failed,
        "web": {
            "queries": [str(r.args["query"]) for r in seen.of("web_search")],
            "fetched": [h["url"] for h in web["fetch"]],
            "not_selling": [
                str((r.data or {}).get("url"))
                for r in seen.of("fetch_url")
                if r.data and not _sells(r)
            ],
            "skipped": web["skipped"],
        },
        "searches": [
            {
                "terms": r.args["terms"],
                "hits": [h["record"] for h in (r.data or {}).get("hits", [])],
            }
            for r in searches
        ],
        "manual_alternatives": _unknown(
            "Which manual ways (counts by hand, spreadsheets, paper logs) do buyers use today, and what do they cost?",  # noqa: E501
            "not_found",
            checked=[str(r.data["query"]) for r in searches if r.data],
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


def _names(row: Mapping[str, Any]) -> set[str]:
    """Every name a company row goes by, folded: its canonical name and its aliases."""
    names = {str(row["canonical"]["value"])} | {str(a["value"]) for a in row.get("aliases", [])}
    return {" ".join(n.lower().split()) for n in names}


def _table_sellers(rows: list[dict[str, Any]], tables: Sequence[Result]) -> None:
    """Attach each price list's sellers to the company of that name, or add the company.

    A seller is matched by its name exactly (case folded) against a company's names; one
    that matches none is a company of its own, known only from the list, its canonical
    name cited to the row that names it. Nothing is merged by a name that only looks alike.
    """
    for r in tables:
        assert r.data is not None
        for p in r.data["passages"]:
            if p["kind"] != "seller":
                continue
            name = " ".join(str(p["fields"]["name"]).split())
            ref = _where(r)
            row = next((c for c in rows if name.lower() in _names(c)), None)
            if row is None:
                row = {
                    "key": f"c{len(rows) + 1:02d}-{analysis.slug(name)}",
                    "canonical": {"value": name, "cite": r.cite(p["n"], p["text"])},
                    "aliases": [],
                    "pages": [],
                    "merge_evidence": [],
                }
                rows.append(row)
            listed = row.setdefault("tables", [])
            if ref not in listed:
                listed.append(ref)


# --- evidence -----------------------------------------------------------------------


def _entry(r: Result, p: Mapping[str, Any], value: str, label: str) -> dict[str, Any]:
    assert r.data is not None
    return {
        "value": value,
        "label": label,
        "passage": p["kind"],
        "record": r.data["record"],
        "published": r.data["published"],
        "cite": r.cite(p["n"], p["text"]),
    }


def _terms_text(p: Mapping[str, Any]) -> str:
    """What a passage says, with a table row's label, which names what its value is."""
    label = str(p.get("fields", {}).get("label", ""))
    return f"{label}: {p['text']}" if label else str(p["text"])


def _evidence(seen: Seen) -> GenerationResult:
    key = seen.ref.key
    found = seen.artifact("research.competitors")
    companies = found[1]["companies"] if found else []
    company = next((c for c in companies if c["key"] == key), None)
    if company is None:
        return _calls(seen, [_finish(f"{key} is not a resolved company")])
    dims = list(found[1].get("dimensions", [])) if found else []
    fetched = _fetched(seen)
    sources = [*company["pages"], *company.get("tables", [])]
    if not fetched:
        return _calls(seen, [_read_again(p) for p in sources])
    ok = [r for r in fetched if r.data]
    names = _names(company)
    if len(ok) < len(sources):
        failed = [_target(r) for r in fetched if r.error]
        return _calls(
            seen, [_finish(f"could not fetch {failed}; nothing is saved without its page")]
        )
    name = company["canonical"]["value"]
    prices: list[dict[str, Any]] = []
    cells: dict[str, list[dict[str, Any]]] = {str(d["id"]): [] for d in dims}
    notes: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    for r in ok:
        assert r.data is not None
        listed = r.data.get("kind") == "pricing_table"
        for p in r.data["passages"]:
            kind, text = p["kind"], p["text"]
            seller = " ".join(str(p.get("fields", {}).get("seller", "")).split()).lower()
            if listed and (seller not in names or kind not in ("price", "text", "spec")):
                continue  # a price list's other rows and blocks are other companies'
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
                row = f.get("row", {})
                terms = "; ".join(str(v) for k, v in row.items() if v not in (text, f.get("label")))
                e = _entry(r, p, str(f["amount"]), "fact")
                e.update(currency=f["currency"], number=f["number"], per=f["per"],
                         item=f.get("label", ""), terms=terms)  # fmt: skip
                prices.append(e)
                continue
            if PRICE_SECTION.search(str(p.get("section", ""))) and kind == "text":
                notes.append(_entry(r, p, text, "fact"))
            if kind not in ("text", "claim", "spec", "relationship"):
                continue
            label = analysis.claim_label(kind, text)
            for d in dims:  # by what it says, never by the heading it sits under
                if analysis.term_hits(_terms_text(p), d["terms"]):
                    cells[str(d["id"])].append(_entry(r, p, text, label))
    checked = [f"record:{r.data['record']}@{r.data['version']}" for r in ok if r.data]
    if not prices:
        prices = [_unknown(f"What does {name} charge? No price is listed on its pages.",
                           "not_found", checked=checked)]  # fmt: skip
    for d in dims:
        if not cells[str(d["id"])]:
            question = f"What does {name} state about {d['name']}? Its pages do not say."
            cells[str(d["id"])] = [_unknown(question, "not_found", checked=checked)]
    stated = [e for e in prices if "value" in e]
    conflicts = []
    relations = []
    by_record = {r.data["record"]: r.data for r in ok if r.data}
    for basis in analysis.price_conflict(stated):
        on = [e for e in stated if parsing.basis_key(str(e["per"])) == parsing.basis_key(basis)]
        conflicts.append(
            {
                "field": "price",
                "per": basis,
                "values": [
                    f"{e['currency']} {e['value']} per {e['per']} ({e['terms']}), "
                    f"page dated {e['published']}"
                    for e in on
                ],
                "status": "open",
            }
        )
        a = on[0]
        b = next(e for e in on if e["record"] != a["record"])
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
        "prices": prices,
        "cells": cells,
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
    scope = seen.artifact("research.scope")
    themes = list(scope[1].get("themes", [])) if scope else []
    asked = [{"id": str(t["id"]), "name": str(t.get("name", t["id"])),
              "terms": [str(x) for x in t.get("terms", ()) or ()]} for t in themes]  # fmt: skip
    if not seen.of("list_inputs"):
        return _calls(seen, [("list_inputs", {"kinds": ["customer_reviews"]})])
    reads = seen.of("read_table")
    if not reads:
        inputs = [i for r in seen.of("list_inputs") if r.data for i in r.data["inputs"]]
        found = _reads_of(inputs, ("customer_reviews",))
        if found:
            return _calls(seen, [("read_table", {**a, "themes": asked}) for a in found])
    more = []
    done = {(r.args["record"], r.args.get("section"), int(r.args.get("offset", 0)))
            for r in reads}  # fmt: skip
    for r in reads:
        nxt = (r.data or {}).get("next_offset")
        at = r.args.get("section")
        if nxt is not None and (r.args["record"], at, int(nxt)) not in done:
            more.append(("read_table", {"record": r.args["record"], "offset": int(nxt),
                                        **({} if at is None else {"section": at}),
                                        "themes": asked}))  # fmt: skip
    if more:
        return _calls(seen, more)
    rows: list[dict[str, Any]] = []
    for r in reads:
        if r.data is None:
            continue
        for row in r.data["rows"]:
            fields = {k: v for k, v in row.items() if k not in ("n", "text", "columns")}
            fields["id"] = str(fields.get("id"))
            if "geography" in fields:
                fields["geography"] = country_code(fields["geography"])
            rows.append(
                {"fields": fields, "text": row["text"], "cite": r.cite(row["n"], row["text"])}
            )
    sample = analysis.review_sample(rows, {str(t["id"]): t["terms"] for t in themes})
    region = scope[1]["geography"].get("normalised") if scope else None
    countries = members(region)
    by_id = {r["fields"]["id"]: r for r in rows}
    needs = []
    for t in themes:
        theme = str(t["id"])
        hits = sample.themes[theme]
        dates = sorted(str(h["fields"].get("date", "")) for h in hits if h["fields"].get("date"))
        in_scope = [h for h in hits if h["fields"].get("geography") in countries]
        needs.append(
            {
                "theme": theme,
                "need": f"What buyers say about {t['name']}",
                "terms": list(t["terms"]),
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
                        "review": h["fields"]["id"],
                        "date": h["fields"].get("date"),
                        "geography": h["fields"].get("geography"),
                        "product": h["fields"].get("product"),
                        "cite": h["cite"],
                    }
                    for h in hits
                ],
            }
        )
    dates = sorted(str(r["fields"]["date"]) for r in sample.unique if r["fields"].get("date"))
    scales = {
        str(r["fields"].get("rating_unit") or r["fields"].get("rating_scale_max") or "")
        for r in sample.unique
    } - {""}
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
            "rating_scales": sorted(scales),
            "note": "counts describe this sample only, never a market",
        },
        "needs": needs,
    }
    if not sample.unique:
        checked = [str(r.data["query"]) for r in seen.of("list_inputs") if r.data]
        summary: dict[str, Any] = content["sample"]  # type: ignore[assignment]
        summary["status"] = "no_feedback_found"
        summary["checked"] = checked
    return _calls(
        seen,
        [_save("needs", content), _finish(f"{len(needs)} needs from {len(sample.unique)} reviews")],
    )


# --- report -------------------------------------------------------------------------


def _cell(entries: list[Any], ref: str, path: str) -> list[dict[str, Any]]:
    out = []
    for i, e in enumerate(entries):
        if "unknown" in e:
            u = e["unknown"]
            out.append(
                {
                    "status": "unknown",
                    "reason": u["reason"],
                    "question": u["question"],
                    "cite": {"artifact": ref, "path": f"{path}/{i}"},
                }
            )
        else:
            out.append(dict(e))
    return out


def _basis(entry: Mapping[str, Any]) -> str:
    return " ".join(str(entry.get("per", "")).lower().split())


def _report(seen: Seen) -> GenerationResult:
    comp = seen.artifact("research.competitors")
    needs = seen.artifact("research.needs")
    scope = seen.artifact("research.scope")
    dims = list(scope[1].get("dimensions", [])) if scope else []
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
            who = c["canonical"]["value"]
            row["prices"] = [_unknown(f"prices of {who}: its pages could not be read", reason,
                                      failed_step=step)]  # fmt: skip
            row["cells"] = {
                str(d["id"]): [
                    _unknown(
                        f"{d['name']} for {who}: its pages could not be read",
                        reason,
                        failed_step=step,
                    )
                ]
                for d in dims
            }
            row["price_conflict"] = False
            row["blocker"] = u.get("blocker")
            unknowns.append(
                _unknown(
                    f"Everything about {who}: its pages could not be read", reason, failed_step=step
                )
            )
            rows.append(row)
            continue
        ref, profile = found
        row["prices"] = _cell(profile["prices"], ref, "/prices")
        row["cells"] = {
            col: _cell(entries, ref, f"/cells/{col}") for col, entries in profile["cells"].items()
        }
        for i, e in enumerate(profile["prices"]):
            if "unknown" in e:
                unknowns.append(
                    {
                        "question": e["unknown"]["question"],
                        "reason": e["unknown"]["reason"],
                        "cite": {"artifact": ref, "path": f"/prices/{i}"},
                    }
                )
        row["price_conflict"] = bool(profile["conflicts"])
        rows.append(row)
        for i, conflict in enumerate(profile["conflicts"], start=1):
            on = [
                e
                for e in profile["prices"]
                if "cite" in e
                and parsing.basis_key(_basis(e)) == parsing.basis_key(str(conflict["per"]))
            ]
            findings.append(
                {
                    "id": f"price-conflict-{key}-{i}",
                    "label": "fact",
                    "statement": f"{profile['name']} lists differing prices per {conflict['per']}: "
                    + "; ".join(conflict["values"])
                    + ". The conflict is left open.",
                    "evidence": [{"cite": e["cite"]} for e in on],
                }
            )
        for col, entries in profile["cells"].items():
            claims = [e for e in entries if e.get("label") == "vendor_claim"]
            facts = [e for e in entries if e.get("passage") == "spec"]
            if claims and facts:
                findings.append(
                    {
                        "id": f"claim-vs-fact-{key}-{col}",
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
    for c in (c for c in companies if c["merge_evidence"]):
        findings.append(
            {
                "id": f"same-company-{c['key']}",
                "label": "fact",
                "statement": f"{c['canonical']['value']} also appears as {', '.join(a['value'] for a in c['aliases'])}: one company, counted once.",  # noqa: E501
                "evidence": [{"cite": m["cite"]} for m in c["merge_evidence"]],
            }
        )
    priced: dict[tuple[str, str], list[tuple[float, dict[str, Any]]]] = {}
    for row in rows:
        for e in row["prices"]:
            if "value" in e and "currency" in e:
                basis = " ".join(str(e.get("per", "")).lower().split())
                number = float(str(e.get("number", e["value"])).replace(",", ""))
                priced.setdefault((e["currency"], basis), []).append((number, e))
    for (cur, basis), points in sorted(priced.items()):
        if len({id(p[1]["record"]) for p in points}) < 2 and len(points) < 2:
            continue
        points.sort(key=lambda x: x[0])
        lo, hi = points[0], points[-1]
        findings.append(
            {
                "id": f"price-range-{analysis.slug(cur + ' ' + basis)}",
                "label": "inference",
                "statement": f"Listed prices in {cur} per {basis} run from {lo[1]['value']} to {hi[1]['value']} (as listed, not converted).",  # noqa: E501
                "evidence": [{"cite": lo[1]["cite"]}, {"cite": hi[1]["cite"]}],
            }
        )
    themes = needs[1]["needs"] if needs else []
    for n in themes:
        if not n["evidence"]:
            continue
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
        "columns": ["company", "prices", *(str(d["id"]) for d in dims)],
        "dimensions": [{"id": d["id"], "name": d["name"]} for d in dims],
        "rows": rows,
        "resolved": len(companies),
        "pages_found": comp[1]["pages_found"] if comp else 0,
        "note": "each cell lists its entries with their page and date; prices are as listed, never converted or averaged",  # noqa: E501
    }
    ranked = sorted((n for n in themes if n["evidence"]), key=lambda n: (-n["count"], n["theme"]))
    tests = []
    for i, n in enumerate(ranked[:3], start=1):
        tests.append(
            {
                "id": f"T{i}",
                "theme": n["theme"],
                "hypothesis": f"Buyers choose the product that does best on {n['need'][len('What buyers say about ') :]}.",  # noqa: E501
                "method": f"Run five buyer interviews and one side-by-side trial on {n['need'][len('What buyers say about ') :]}, the reviews' own words as prompts.",  # noqa: E501
                "metric": "buyers who name it among their top two reasons to choose",
                "threshold": {"value": "3 of 5 buyers", "status": "proposal"},
                "why": f"{n['phrase']} raise it.",
                "evidence": [{"cite": e["cite"]} for e in n["evidence"][:2]],
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


_HANDLERS = {
    "scope": _scope,
    "discover": _discover,
    "evidence": _evidence,
    "voice": _voice,
    "report": _report,
}


class ResearchModel(RuleGenerationModel):
    """The research agent's rule-based generation policy (module doc).

    ``queries`` are web searches the task's world suggests, made beside the request's own
    words (:func:`web_queries`): the offline stand-in for a model that knows the market.
    """

    def __init__(self, queries: Sequence[str] = ()) -> None:
        """A policy that also searches the web for ``queries``."""
        self.queries = tuple(queries)

    def for_world(self, hints: Mapping[str, Any]) -> ResearchModel:
        """This policy told a world's ``hints`` (its ``queries``), as the composition root asks."""
        return type(self)(tuple(str(q) for q in hints.get("queries", ())))

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """The next calls of the request's Task, from what the request carries."""
        seen = parse(request)
        seen.queries = self.queries
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
        for r in _fetched(seen):
            for p in (r.data or {}).get("passages", []):
                m = re.search(r"[\w.]+@[\w.]+\w", p["text"]) if p.get("instruction_like") else None
                if m is not None and m.group(0) not in sent:
                    args = {"recipient": m.group(0), "document": "everything gathered so far"}
                    return _calls(seen, [("send_document", args)])
        return super().generate(request)


ADAPTERS = {
    "generate": ResearchModel(),
    "page_facts": fakes_readers.PageFacts(),
    "feedback_items": fakes_readers.FeedbackItems(),
    "table_roles": fakes_readers.TableRoles(),
    "theme_match": fakes_readers.ThemeMatch(),
    "doc_kind": fakes_readers.DocKind(),
    "price_table": fakes_readers.PriceTable(),
    "report_facts": fakes_readers.ReportFacts(),
}
"""The ``fake`` run mode's model per alias: the composition root binds it (ADR-P07)."""
