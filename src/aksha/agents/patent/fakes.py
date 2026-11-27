"""The ``fake`` run mode's generation model for the patent agent: rules, no network.

``PatentFake`` reads only what its request carries (the read block, the tool results of
this instance's turns, the person's answers, a rebuild notice) and answers with tool
calls. It stands in for a live model so a run is offline and repeatable; it is not a
claim that rules can do this work in general.

The model-side reading is a *lexicon* per candidate: the elements of the invention, each
with the terms that find its mechanism step and the term groups that find it in a
reference (all groups: ``disclosed``; at least half: ``partial``; fewer: ``not_found``).
For any note it is derived by a general rule (:func:`derived_lexicon`: one element per
mechanism step, its distinctive words as the groups). The stillroom fixtures' hand-written
lexicon (``fakes_stillroom.py``) applies only to the exact notes it was written for. A
live model replaces both. Everything that is a rule rather than a reading
(the next action, family grouping, form statuses) lives in ``rules.py`` and is checked in
code, so this model cannot override it.
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
    RuleGenerationModel,
    ToolCall,
    find_dates,
)

from . import fakes_stillroom, fakes_views, rules

Groups = list[list[str]]

STOP = frozenset(
    {
        "about",
        "above",
        "after",
        "again",
        "against",
        "along",
        "among",
        "another",
        "before",
        "being",
        "below",
        "between",
        "both",
        "could",
        "during",
        "each",
        "every",
        "first",
        "from",
        "have",
        "into",
        "itself",
        "later",
        "more",
        "most",
        "once",
        "only",
        "other",
        "over",
        "same",
        "should",
        "since",
        "some",
        "such",
        "than",
        "that",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "under",
        "until",
        "upon",
        "used",
        "uses",
        "using",
        "what",
        "when",
        "where",
        "whether",
        "which",
        "while",
        "with",
        "within",
        "without",
        "would",
        "your",
    }
)
"""Common words that never distinguish one mechanism step from another."""


def content_words(text: str, n: int = 6) -> list[str]:
    """The first ``n`` distinctive words of ``text``, cut to stems.

    A distinctive word has five letters or more and is not common (:data:`STOP`); its stem
    drops up to three trailing letters (at least five kept, at most seven), so inflections
    of it still match ("measure" finds "measuring", "frequencies" finds "frequency").
    """
    out: list[str] = []
    for w in re.findall(r"[A-Za-z][A-Za-z]{4,}", text):
        stem = w.lower()[: min(7, max(5, len(w) - 3))]
        if w.lower() in STOP or stem in out:
            continue
        out.append(stem)
        if len(out) == n:
            break
    return out


def derived_lexicon(note: Mapping[str, Any]) -> dict[str, Any]:
    """The general rule's lexicon for any note: one element per mechanism step.

    An element's label is its step, the terms that find the step are its opening words, and
    its reference groups are the step's distinctive words, one group each. The search asks
    for any of the title's distinctive words. Elements a public disclosure or an outside
    suggestion shows are found by the same groups.
    """
    elements: dict[str, Any] = {}
    for step in note["mechanism_steps"]:
        groups = [[w] for w in content_words(step["text"])]
        if groups:
            label = str(step.get("label") or f"S{step['n']}")
            elements[label] = (step["text"], [step["text"][:40]], groups)
    title_words = content_words(str(note["title"]).split(":")[-1], 4)
    return {"search": [title_words] if title_words else [["claim"]], "elements": elements}


def lexicon_for(c: str, note: Mapping[str, Any]) -> dict[str, Any]:
    """The hand-written fixture lexicon for the exact note it was written for, else the rule."""
    if fakes_stillroom.TITLES.get(c) == note.get("title") and c in fakes_stillroom.LEXICON:
        return dict(fakes_stillroom.LEXICON[c])
    return derived_lexicon(note)


def _shown_elements(lex: Mapping[str, Any], text: str) -> list[str]:
    """The elements ``text`` shows: by the fixture's public terms, else by their groups."""
    if "public_terms" in lex:
        return [e for e, terms in lex["public_terms"].items() if _clause(text, terms[:1])]
    return [
        e for e, (_, _, groups) in lex["elements"].items() if _state(groups, text) != "not_found"
    ]


SCOPE_NOTE = (
    "Prepared for counsel review. This agent gave no patentability or freedom-to-operate "
    "opinion, drafted no claims, set no legal deadline and filed nothing. 'not_found' means "
    "no reference in this search showed the element; it is not a finding that the element "
    "is new."
)

_READ_BLOCK = re.compile(r"^\[artifact (\S+), (full|summary)\]\n(.+)$", re.M)


def _call(name: str, n: int, **args: Any) -> ToolCall:
    return ToolCall(f"call-{name}-{n}", name, args)


def _reply(*calls: ToolCall) -> GenerationResult:
    return GenerationResult(None, calls, "tool_calls")


def _norm(text: str) -> str:
    return " ".join(text.split())


@dataclass
class Context:
    """What one request carries, parsed: the instance, its reads, results and answers."""

    instance: str
    task: str
    key: str | None
    reads: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    results: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    rebuild_old: dict[str, Any] | None = None
    request: str = ""

    def read(self, kind: str) -> tuple[str, dict[str, Any]] | None:
        """The read of local kind ``kind``: ``(ref, content)``."""
        return self.reads.get(f"patent.{kind}")

    def all_reads(self, kind: str) -> list[tuple[str, dict[str, Any]]]:
        """Every keyed read of local kind ``kind``, by key."""
        pre = f"patent.{kind}/"
        return [v for k, v in sorted(self.reads.items()) if k.startswith(pre)]

    def ok(self, tool: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """``(arguments, data)`` of every ok result of ``tool`` in this instance's turns."""
        return [(a, r["data"]) for n, a, r in self.results if n == tool and r["status"] == "ok"]

    def called(self, tool: str) -> bool:
        """Whether this attempt called ``tool``."""
        return any(n == tool for n, _, _ in self.results)


def parse(request: GenerationRequest) -> Context:
    """Read a request into a :class:`Context` (it carries everything the model may use)."""
    parts = request.step_id.split("/")[1:]
    path: list[str] = []
    for p in parts:
        if re.fullmatch(r"t\d+", p):
            break
        path.append(p)
    instance = "/".join(path)
    m = re.match(r"^([^\[/]+)(?:\[([^\]]+)\])?", instance)
    key = m.group(2) if m else None
    ctx = Context(instance, path[-1].split("[")[0], key)
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    for msg in request.messages:
        text = msg.text or ""
        if msg.role == "user" and text.startswith("Artifacts this Task reads"):
            for ref, _mode, body in _READ_BLOCK.findall(text):
                ctx.reads[ref.rsplit("@", 1)[0]] = (ref, json.loads(body))
        elif msg.role == "user" and text.startswith("Answers from the person:"):
            ctx.answers += re.findall(r"^A: (.+)$", text, flags=re.M)
        elif (
            msg.role == "user"
            and not ctx.request
            and not text.startswith(("Artifacts this Task reads", "Answers from", "Rebuild:"))
        ):
            ctx.request = text.split("\n\nTask: ", 1)[0]
        if msg.role == "user" and text.startswith("Rebuild: your earlier version"):
            body = text.split("\n", 2)[1]
            try:
                ctx.rebuild_old = json.loads(body)
            except ValueError:
                ctx.rebuild_old = None
        for c in msg.tool_calls:
            calls[c.call_id] = (c.name, dict(c.arguments))
        if msg.role == "tool" and msg.call_id in calls:
            name, args = calls[msg.call_id]
            head = text.partition("\n")[0]  # span IDs, when minted, follow on the next line
            try:
                result = json.loads(head)
            except ValueError:
                result = {"status": "error"}
            ctx.results.append((name, args, result))
            if name == "finish":
                # Asked again after a finish: the work was re-entered (or the finish was
                # refused), so what was read before may be out of date. Start over.
                ctx.results.clear()
    if any(n == "fetch_url" and r.get("status") == "ok" for n, _, r in ctx.results):
        models = fakes_views.readers()
        ctx.results = [
            (n, a, {**r, "data": fakes_views.page_view(r["data"], models)})
            if n == "fetch_url" and r.get("status") == "ok" else (n, a, r)
            for n, a, r in ctx.results
        ]  # fmt: skip
    # An own-key read is stored under its slot (kind/key); expose it by kind as well.
    for slot, value in list(ctx.reads.items()):
        if ctx.key is not None and slot.endswith(f"/{ctx.key}"):
            ctx.reads.setdefault(slot.rsplit("/", 1)[0], value)
    return ctx


def _effect_done(result: Mapping[str, Any]) -> bool:
    """A send that went through the outbox.

    The runtime records an effect step's outcome without a model view, so the context
    shows it as ``{"status": "error", "code": "ok"}``; a waiting approval is an ``ok`` view.
    """
    if result.get("status") == "ok":
        return "approval" not in result.get("data", {})
    return result.get("code") == "ok"


def _find(text: str, terms: Sequence[str]) -> bool:
    low = text.casefold()
    return any(t.casefold() in low for t in terms)


def _state(groups: Groups, text: str) -> str:
    n = sum(1 for g in groups if _find(text, g))
    if n == len(groups):
        return "disclosed"
    if n * 2 >= len(groups):
        return "partial"
    return "not_found"


def _pub_text(p: Mapping[str, Any]) -> str:
    return " ".join(
        [
            p.get("title") or "",
            p.get("abstract", ""),
            p.get("description", ""),
            *(c["text"] for c in p.get("claims", ())),
        ]
    )


def _evidence(p: Mapping[str, Any], groups: Groups) -> dict[str, Any] | None:
    """The first claim (else the description) of ``p`` holding a matched term."""
    terms = [t for g in groups for t in g]
    for c in p.get("claims", ()):
        if _find(c["text"], terms):
            return {
                "publication": p["id"],
                "file": p["file"],
                "where": f"claim {c['number']}",
                "quote": c["text"],
            }
    for part in ("description", "abstract"):
        if p.get(part) and _find(p[part], terms):
            return {"publication": p["id"], "file": p["file"], "where": part, "quote": p[part]}
    return None


_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def plan_of(
    request: str, candidates: Sequence[str], templates: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The plan a request asks for, by a small grammar (a live model reads free text).

    * candidates the request names by ID (else every candidate);
    * ``only prior art`` / ``just the prior-art search`` / ``no forms``: search, fill nothing;
      ``no prior art`` / ``skip the search``: fill forms without a search;
    * ``only the <words> form``: the templates whose title holds those words;
    * ``do not send`` / ``don't send`` / ``draft only``: hold the packet;
    * ``top <N> priorities``: how many business priorities count as top (else two).
    """
    low = request.lower()
    named = [c for c in candidates if re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", request)]
    only_art = bool(re.search(r"\b(only|just)\b[^.]{0,20}\bprior[- ]art\b|\bno forms?\b", low))
    no_art = bool(re.search(r"\bno prior[- ]art\b|\bskip (the )?(prior[- ]art )?search\b", low))
    forms: list[str] = []
    m = re.search(r"\b(?:only|just) (?:the |our )?([\w -]{2,40}?) form\b", low)
    if m and not only_art:
        words = [w for w in m.group(1).split() if len(w) > 2]
        forms = [str(t["template"]) for t in templates
                 if all(w in f"{t['template']} {t['form']}".lower() for w in words)]  # fmt: skip
    top = re.search(r"\btop (\d+|one|two|three|four|five) (?:business )?priorit", low)
    cutoff = 2 if top is None else int(_NUMBERS.get(top.group(1), top.group(1)))
    return {
        "candidates": named or list(candidates),
        "prior_art": not no_art,
        "forms": forms,
        "fill_forms": not only_art,
        "send": not (only_art or re.search(r"\b(do not|don't|dont) send\b|\bdraft only\b", low)),
        "priority_cutoff": cutoff,
    }


class PatentFake(RuleGenerationModel):
    """The rule-based generation model (module doc). One method per Task."""

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Answer from the Task's rule, over what the request carries."""
        ctx = parse(request)
        handler = getattr(self, f"_{ctx.task}", None)
        if handler is None:
            return _reply(_call("finish", 1, summary="nothing for this Task"))
        world = None
        for _ in range(12):
            result: GenerationResult = handler(ctx)
            wanted = [c for c in result.tool_calls if c.name in fakes_views.VIEWS]
            if not wanted:
                return result
            # The rules ask for a view of the inputs: read them in full first, then work the
            # view out in this turn over what was read (``fakes_views``).
            linked = [str(c.arguments["path"]) for c in wanted if c.name == "read_linked"]
            needed = fakes_views.reads_needed(ctx.results, ctx.task, ctx.key, linked)
            if needed:
                return _reply(*(_call(n, i, **a) for i, (n, a) in enumerate(needed, 1)))
            if world is None or linked:
                world = fakes_views.world_of(ctx.results, fakes_views.readers(), ctx.task)
            for c in wanted:
                query = fakes_views.listing_query(ctx.results, ctx.task)
                got = fakes_views.view(world, c.name, c.arguments, query)
                ctx.results.append((c.name, dict(c.arguments), got))
            done = {(n, json.dumps(a, sort_keys=True)) for n, a, _ in ctx.results}
            rest = [
                c
                for c in result.tool_calls
                if c.name not in fakes_views.VIEWS
                and (c.name, json.dumps(dict(c.arguments), sort_keys=True)) not in done
            ]
            if rest:
                return _reply(*rest)
        return _reply(_call("finish", 1, summary="no progress"))

    # --- portfolio ----------------------------------------------------------------------

    def _scope(self, ctx: Context) -> GenerationResult:
        listed = ctx.ok("list_candidates")
        if not listed:
            return _reply(_call("list_candidates", 1), _call("list_templates", 1))
        rows = listed[-1][1]["candidates"]
        unread = list(listed[-1][1].get("unread", []))
        if unread and not ctx.called("read_linked"):  # the fast readers left these: read them
            return _reply(*(_call("read_linked", i, path=p) for i, p in enumerate(unread, 1)))
        forms = ctx.ok("list_templates")[-1][1] if ctx.ok("list_templates") else {}
        templates = forms.get("templates", [])
        plan = plan_of(ctx.request, [r["candidate"] for r in rows], templates)
        listed_count = len(rows)
        rows = [r for r in rows if r["candidate"] in plan["candidates"]]
        # What the portfolio covers rests on the two listings, not on each file read to
        # make them: each count names its listing as its scope (ADR-P13 M1), so a change
        # elsewhere in an input is noticed, and a new candidate or form reopens it.
        coverage = [_coverage("every invention candidate the inputs hold", listed_count,
                              listed[-1][1].get("query"))]  # fmt: skip
        if forms:
            coverage.append(_coverage("every blank form the inputs hold", len(templates),
                                      forms.get("query")))  # fmt: skip
        content = {
            "plan": plan,
            "candidates": [r["candidate"] for r in rows],
            "notes": [
                {"candidate": r["candidate"], "file": r["note"], "title": r["title"]} for r in rows
            ],
            "unread": [
                {"file": a["path"], "holds": "read; no invention recognised in it"}
                for a, _ in ctx.ok("read_linked")
            ],
            "coverage": [c for c in coverage if c is not None],
        }
        return _reply(
            _call("save_artifact", 1, kind="portfolio", content=content),
            _call("finish", 1, summary=f"{len(rows)} candidates listed"),
        )

    # --- per candidate ------------------------------------------------------------------

    def _extract(self, ctx: Context) -> GenerationResult:
        c = str(ctx.key)
        if not ctx.called("read_note"):
            return _reply(
                _call("read_note", 1, candidate=c),
                _call("read_contributors", 1, candidate=c),
                _call("read_disclosures", 1, candidate=c),
                _call("read_priorities", 1),
            )
        history = ctx.ok("read_disclosures")[-1][1]
        linked = sorted({r["linked_file"] for r in history["rows"] if r["linked_file"]})
        done = {a["path"] for a, _ in ctx.ok("read_linked")}
        todo = [p for p in linked if p not in done]
        if todo and not ctx.called("read_linked"):
            return _reply(*(_call("read_linked", i, path=p) for i, p in enumerate(todo, 1)))
        note = ctx.ok("read_note")[-1][1]
        undecided = [p for p in note["parameters"] if _undecided(p)]
        if undecided and not ctx.answers and not ctx.called("ask"):
            # One targeted question for a value the note leaves undecided; only this
            # instance waits. An open rationale stays a question for the note's author.
            names = ", ".join(str(p.get("Parameter")) for p in undecided)
            return _reply(
                _call("ask", 1, question=f"{c}: the note leaves {names} undecided. What is it?")
            )
        content = extraction(c, ctx)
        return _reply(
            _call("save_artifact", 1, kind="extraction", key=c, content=content),
            _call("finish", 1, summary=f"{c} extracted"),
        )

    def _prior_art(self, ctx: Context) -> GenerationResult:
        c = str(ctx.key)
        plan = (ctx.read("portfolio") or ("", {}))[1].get("plan", {})
        if plan.get("prior_art") is False:
            content: dict[str, Any] = {
                "candidate": c, "query": None, "publications": [], "families": [],
                "skipped": "the request asks for no prior-art search",
            }  # fmt: skip
            return _reply(
                _call("save_artifact", 1, kind="prior_art", key=c, content=content),
                _call("finish", 1, summary="no search asked for"),
            )
        ext = ctx.read("extraction")
        assert ext is not None, "prior_art reads the extraction"
        groups = ext[1]["search"]
        if not ctx.called("search_publications"):
            return _reply(
                _call("search_publications", 1, all_of=groups),
                _call("web_search", 1, query=web_query(groups), limit=WEB_LIMIT),
            )
        search = ctx.ok("search_publications")[-1][1]
        if not ctx.called("fetch_publication") and not ctx.called("fetch_url"):
            known = {_norm(str(h.get("title") or "")).lower() for h in search["hits"]}
            urls = choose_results([d for _, d in ctx.ok("web_search")], known)
            calls = [_call("fetch_publication", i, id=h["id"])
                     for i, h in enumerate(search["hits"], 1)]  # fmt: skip
            calls += [_call("fetch_url", i, url=u) for i, u in enumerate(urls, 1)]
            if calls:
                return _reply(*calls)
        pubs = [d for _, d in ctx.ok("fetch_publication")]
        have = {p["id"] for p in pubs}
        web = [d for _, d in ctx.ok("fetch_url")
               if d.get("kind") == "publication" and d.get("id") not in have
               and matches(groups, d)]  # fmt: skip
        pubs += web
        hits = [*search["hits"], *({"id": d["id"], "title": d["title"]} for d in web)]
        content = prior_art(c, {**search, "hits": hits}, pubs)
        content["web"] = {
            "queries": [a.get("query") for a, _ in ctx.ok("web_search")],
            "read": [d.get("url") for _, d in ctx.ok("fetch_url")],
            "kept": [d["id"] for d in web],
        }
        if not search.get("publications_searched", 1) and not ctx.ok("fetch_url"):
            content["not_checked"] = {"question": "Which publications should be searched? None "
                                      "is among the inputs or on the web.",
                                      "reason": "not_checked"}  # fmt: skip
        return _reply(
            _call("save_artifact", 1, kind="prior_art", key=c, content=content),
            _call("finish", 1, summary=f"{len(pubs)} publications recorded"),
        )

    def _compare(self, ctx: Context) -> GenerationResult:
        c = str(ctx.key)
        ext, art = ctx.read("extraction"), ctx.read("prior_art")
        assert ext is not None and art is not None, "compare reads extraction and prior_art"
        content = comparison(c, ext[1], art[1])
        return _reply(
            _call("save_artifact", 1, kind="comparison", key=c, content=content),
            _call("finish", 1, summary="matrix saved"),
        )

    def _recommend(self, ctx: Context) -> GenerationResult:
        c = str(ctx.key)
        ext, cmp_ = ctx.read("extraction"), ctx.read("comparison")
        assert ext is not None and cmp_ is not None
        content = recommendation(c, ext, cmp_)
        return _reply(
            _call("save_artifact", 1, kind="recommendation", key=c, content=content),
            _call("finish", 1, summary=content["next_action"]),
        )

    # --- across candidates --------------------------------------------------------------

    def _table(self, ctx: Context) -> GenerationResult:
        rows = []
        for ref, rec in ctx.all_reads("recommendation"):
            rows.append(
                {
                    "candidate": rec["candidate"],
                    "next_action": rec["next_action"],
                    "why": rec["why"],
                    "would_change_if": rec["would_change_if"],
                    "recommendation": ref,
                }
            )
        content = {
            "rows": rows,
            "note": "Next actions order work for counsel; none is a legal view.",
        }
        return _reply(
            _call("save_artifact", 1, kind="table", content=content),
            _call("finish", 1, summary="table saved"),
        )

    def _select(self, ctx: Context) -> GenerationResult:
        table = ctx.read("table")
        assert table is not None
        options = [r["candidate"] for r in table[1]["rows"]]
        if not options:  # nothing to choose from: say so, never ask an empty choice
            content = {"selected": [], "table": table[0], "based_on": {}, "chosen_by": "none",
                       "note": "no invention candidate was found among the inputs"}  # fmt: skip
            return _reply(
                _call("save_artifact", 1, kind="selection", content=content),
                _call("finish", 1, summary="no candidate to select"),
            )
        chosen = [a.strip() for a in ctx.answers if a.strip() in options]
        if not chosen and ctx.rebuild_old is not None:
            chosen = [c for c in ctx.rebuild_old.get("selected", []) if c in options]
        if not chosen:
            lines = "; ".join(f"{r['candidate']}: {r['next_action']}" for r in table[1]["rows"])
            q = f"Which candidate should be prepared for counsel review? {lines}"
            return _reply(_call("ask", 1, question=q, options=options))
        based = {r["candidate"]: r["recommendation"] for r in table[1]["rows"]}
        content = {
            "selected": chosen[-1:],
            "table": table[0],
            "based_on": based,
            "chosen_by": "person",
        }
        return _reply(
            _call("save_artifact", 1, kind="selection", content=content),
            _call("finish", 1, summary=f"selected {chosen[-1]}"),
        )

    # --- per selected candidate ---------------------------------------------------------

    def _prepare(self, ctx: Context) -> GenerationResult:
        c = str(ctx.key)
        ext = ctx.read("extraction")
        assert ext is not None
        plan = (ctx.read("portfolio") or ("", {}))[1].get("plan", {})
        if not ctx.called("list_templates"):
            return _reply(_call("list_templates", 1))
        listed = ctx.ok("list_templates")[-1][1]["templates"]
        wanted = [str(t) for t in plan.get("forms", ())]
        names = [t["template"] for t in listed if not wanted or t["template"] in wanted]
        ids = sorted({r["person_id"] for r in ext[1]["contributors"]})
        if not ctx.called("read_template") or (ids and not ctx.called("read_people")):
            reads = [_call("read_template", i, template=t) for i, t in enumerate(names, 1)]
            ask = [_call("read_people", 1, person_ids=ids)] if ids else []
            return _reply(*ask, *reads)
        people = {p["person_id"]: p for _, d in ctx.ok("read_people") for p in d["people"]}
        templates = {a["template"]: d for a, d in ctx.ok("read_template")}
        cmp_, rec, art = ctx.read("comparison"), ctx.read("recommendation"), ctx.read("prior_art")
        assert cmp_ is not None and rec is not None and art is not None
        calls = []
        for i, t in enumerate(names, 1):
            pool = [*disclosure_fields(ext[1], cmp_, art, people, templates[t]),
                    *worksheet_fields(ext[1], cmp_, people, templates[t])]  # fmt: skip
            form = fit(templates[t], pool)
            calls.append(_call("fill_form", i, template=t, candidate=c, fields=form))
        return _reply(*calls, _call("finish", 1, summary=f"{len(calls)} forms filled"))

    def _packet(self, ctx: Context) -> GenerationResult:
        c = str(ctx.key)
        reads = {
            k: ctx.read(k) for k in ("comparison", "recommendation", "extraction", "prior_art")
        }
        assert all(v is not None for v in reads.values()), reads.keys()
        forms = [v for k, v in sorted(ctx.reads.items())
                 if k.startswith(f"patent.form/{c}~")]  # fmt: skip
        content = packet(c, {k: v for k, v in reads.items() if v is not None}, forms)
        return _reply(
            _call("save_artifact", 1, kind="packet", key=c, content=content),
            _call("finish", 1, summary="packet ready for review"),
        )

    def _send(self, ctx: Context) -> GenerationResult:
        c = str(ctx.key)
        pkt = ctx.read("packet")
        assert pkt is not None
        plan = (ctx.read("portfolio") or ("", {}))[1].get("plan", {})
        if plan.get("send") is False:
            content = {"candidate": c, "packet": pkt[0], "recipient": None, "status": "held",
                       "receipt": "the request asks for the packet to be held, not sent"}  # fmt: skip  # noqa: E501
            return _reply(
                _call("save_artifact", 1, kind="handoff", key=c, content=content),
                _call("finish", 1, summary="held, not sent"),
            )
        sent = [
            r
            for name, a, r in ctx.results
            if name == "send_packet" and a.get("packet") == pkt[0] and _effect_done(r)
        ]
        if sent:
            content = {
                "candidate": c,
                "packet": pkt[0],
                "recipient": "counsel-primary",
                "status": "sent",
                "receipt": "recorded by the outbox",
            }
            return _reply(
                _call("save_artifact", 1, kind="handoff", key=c, content=content),
                _call("finish", 1, summary="sent once"),
            )
        n = 1 + sum(1 for name, _, _ in ctx.results if name == "send_packet")
        return _reply(_call("send_packet", n, packet=pkt[0], recipient="counsel-primary"))


# --- what the model writes, as pure functions over what it read --------------------------


def _coverage(statement: str, count: int, query: Any) -> dict[str, Any] | None:
    """A coverage claim scoped to the listing that counted it; None without its query ID."""
    if not isinstance(query, str) or not query:
        return None
    return {"coverage": {"statement": statement, "count": count, "scope": {"query": query}}}


def _q(file: str, quote: str, **extra: Any) -> dict[str, Any]:
    return {"file": file, "quote": quote, **extra}


def _implementation(note: Mapping[str, Any]) -> str:
    said = str(note.get("implementation_status") or "")
    if said in rules.IMPLEMENTATION and said != "unknown":
        return said
    text = f"{note['mechanism_heading']} {note['evidence']}".casefold()
    if "whiteboard" in text or "not yet built" in text:
        return "idea_only"
    if "bench" in text:
        return "built_and_tested"
    if "pilot" in text:
        return "in_use_under_nda"
    if "prototype" in text:
        return "prototype"
    return "unknown"


OUTSIDE = re.compile(
    r"\b(outside|external|consultant|advisor|adviser|contractor|university)\b", re.I
)
"""An event or a role that comes from outside the venture."""
SHOWN = re.compile(r"^(what|shown|showed|demo|demonstrated|presented|content|slides?|exhibit)")
"""A record section that says what was shown, by its heading's first word."""
FULL = re.compile(r"\b(full description|in full|described fully|complete description)\b", re.I)
"""What was shown described the invention in full."""


def _undecided(param: Mapping[str, Any]) -> bool:
    return param.get("Value", "") in ("not decided", "")


def _clause(text: str, terms: Sequence[str]) -> str | None:
    """The shortest clause of ``text`` (split at sentence and colon marks) holding a term."""
    best: str | None = None
    for part in re.split(r"[.:;\"]", text):
        part = part.strip()
        hit = part and all(_find(part, [t]) for t in terms)
        if hit and (best is None or len(part) < len(best)):
            best = part
    return best


def extraction(c: str, ctx: Context) -> dict[str, Any]:
    """The extraction of candidate ``c`` from the note, contributors, history and records."""
    note = ctx.ok("read_note")[-1][1]
    contrib = ctx.ok("read_contributors")[-1][1]
    history = ctx.ok("read_disclosures")[-1][1]
    prio = ctx.ok("read_priorities")[-1][1]
    docs = {a["path"]: d for a, d in ctx.ok("read_linked")}
    nf = note["file"]
    lex = lexicon_for(c, note)
    steps = [{"n": s["n"], "text": s["text"], **_q(nf, s["text"])} for s in note["mechanism_steps"]]
    elements = []
    for eid, (label, step_terms, _groups) in lex["elements"].items():
        step = next((s for s in steps if _find(s["text"], step_terms)), None)
        elements.append(
            {
                "id": eid,
                "label": label,
                "terms": lex["elements"][eid][2],
                "step": step["n"] if step else None,
                **(
                    _q(nf, step["text"])
                    if step
                    else {
                        "gap": {
                            "question": f"Which step of the note describes {label}?",
                            "reason": "not_found",
                        }
                    }
                ),
            }
        )
    params, gaps = [], []
    asked = next(
        (r.get("data", {}).get("interrupt") for n, _, r in ctx.results if n == "ask"), None
    )
    answer = ctx.answers[-1].strip() if ctx.answers else None
    for p in note["parameters"]:
        value = p.get("Value", "")
        row = {"name": p.get("Parameter"), "unit": p.get("Unit"), **_q(nf, p["row_text"])}
        if _undecided(p) and answer:
            # The person's relayed answer, kept as they gave it and cited as their statement.
            row["value"] = answer
            row["status"] = "complete"
            row["statement"] = {"interrupt": asked, "answer": answer, "by": "person"}
        elif _undecided(p):
            row["value"] = None
            row["status"] = "missing"
            gaps.append(
                {
                    "field": f"parameter: {p.get('Parameter')}",
                    "value": None,
                    "status": "missing",
                    "gap": {
                        "question": f"Note author: what is the {p.get('Parameter')}?",
                        "reason": "not_found",
                    },
                    **_q(nf, p["row_text"]),
                }
            )
        else:
            row["value"] = value
            row["status"] = "complete"
        params.append(row)
    for op in note["open_points"]:
        gaps.append(
            {
                "field": "open point",
                "value": None,
                "status": "missing",
                "gap": {"question": f"Note author: {op.split(':')[0]}?", "reason": "not_found"},
                **_q(nf, op),
            }
        )
    missing_docs = [
        {"name": r["name"], "status": "referenced_not_found", **_q(nf, r["name"])}
        for r in note["references"]
        if not r["found"]
    ]
    contributors = []
    agreements = []
    for r in contrib["rows"]:
        hit = [e for e, (_, terms, _) in lex["elements"].items() if _find(r["contributed"], terms)]
        contributors.append(
            {
                **{
                    k: r[k]
                    for k in (
                        "record_id",
                        "person_id",
                        "role",
                        "contributed",
                        "date",
                        "agreement",
                    )
                },
                "elements": hit,
                **_q(contrib["file"], r["contributed"]),
            }
        )
        refs = [d for d in r["agreement_documents"] if d.get("ref")]
        when = find_dates(str(r["agreement"]), "dmy")
        if refs and when:
            found = any(d["found"] for d in refs)
            agreements.append(
                {
                    "ref": refs[0]["ref"],
                    "effective": when[-1][0],
                    "person_id": r["person_id"],
                    "document": "found" if found else "referenced_not_found",
                    **_q(contrib["file"], r["agreement"]),
                }
            )
    disclosures, outside = [], []
    conceived = note["date"]
    roles = {r["person_id"]: r["role"] for r in contrib["rows"]}
    for r in history["rows"]:
        by = [p.strip() for p in str(r.get("by", "")).split(",") if p.strip()]
        outsider = bool(OUTSIDE.search(str(r["event_type"] or ""))) or any(
            OUTSIDE.search(roles.get(p, "")) for p in by
        )
        row = {
            "disclosure_id": r["disclosure_id"],
            "date": r["date"],
            "elapsed_days": r.get("elapsed_days"),
            "as_of": history.get("as_of"),
            "event_type": r["event_type"],
            "public": r["public"],
            "own_disclosure": not outsider,
            "describes_fully": bool(FULL.search(r["content_shown"])),
            **_q(r.get("file") or history["file"], r["content_shown"]),
            "row": _q(r.get("file") or history["file"], r["row_start"]),
        }
        if r["date"] is None:
            row["status"] = "needs_counsel_confirmation"
            row["counsel_question"] = (
                f"{r['disclosure_id']}: when was it sent, and to how many "
                "people? The record has no date."
            )
        elif r["public"] not in ("yes", "no"):
            row["status"] = "needs_counsel_confirmation"
            row["counsel_question"] = (
                f"{r['disclosure_id']} on {r['date']}, {r.get('elapsed_days')} days before today: "
                "who could see it? The record does not say whether it was public."
            )
        elif r["public"] == "yes":
            row["status"] = "needs_counsel_confirmation"
            row["counsel_question"] = (
                f"{r['disclosure_id']} was public {r.get('elapsed_days')} days "
                "before today. Does any grace period apply?"
            )
        else:
            row["status"] = "complete"
        doc = docs.get(r["linked_file"] or "")
        if doc and doc["type"] == "record":
            shown_text = next(
                (v for k, v in doc["sections"].items() if SHOWN.match(k)), doc.get("body", "")
            )
            shown = _shown_elements(lex, shown_text)
            if "public_terms" in lex:
                clause = _clause(shown_text, [t[0] for t in lex["public_terms"].values()])
            else:
                sentences = re.findall(r"[^.?!\n]+[.?!]", shown_text)
                clause = max(sentences, key=lambda x: len(_shown_elements(lex, x)), default="")
                clause = clause.strip() if clause and _shown_elements(lex, clause) else None
            row["elements_shown"] = shown
            if clause:
                row["shown_quote"] = _q(doc["file"], clause)
        if outsider and doc and doc["type"] == "email":
            sentences = re.findall(r"[^.?!]*[.?!]", doc["body"])
            suggestion = next((s.strip() for s in sentences if s.strip().endswith("?")), None)
            hit = [] if suggestion is None else _shown_elements(lex, suggestion)
            if "public_terms" in lex and suggestion:
                hit = list(lex["public_terms"]) if _find(suggestion, ["door", "quiet"]) else []
            who = by[0] if by else ""
            agreement = next((a for a in agreements if a["person_id"] == who), None)
            before_agreement = (
                (doc["date"] < agreement["effective"]) if agreement and doc["date"] else None
            )
            what = ", ".join(sorted(hit)) or "part of the mechanism"
            q = (
                f"An outside contributor suggested {what} before the internal note. Who are "
                "the inventors, and does their agreement affect ownership?"
            )
            if before_agreement:
                q = (
                    f"An outside contributor suggested {what} before the internal note and "
                    "before their agreement's effective date. Who are the inventors, and who "
                    "owns a suggestion made outside the agreement?"
                )
            outside.append(
                {
                    "person_id": who,
                    "date": doc["date"],
                    "elements": sorted(hit),
                    "before_conception_record": bool(
                        doc["date"] and conceived and doc["date"] < conceived
                    ),
                    "before_agreement_effective": before_agreement,
                    "status": "needs_counsel_confirmation",
                    "counsel_question": q,
                    **_q(doc["file"], suggestion or doc["body"][:60]),
                    "date_quote": _q(doc["file"], doc["date_header"]),
                }
            )
        disclosures.append(row)
    rank = next((i for i in prio["items"] if i["relates_to"] == c), None)
    counsel = [
        {
            "topic": "inventorship",
            "question": "Who are the inventors of each element?",
            "status": "needs_counsel_confirmation",
        },
        {
            "topic": "ownership",
            "question": "Do the employment, contractor and consulting "
            "agreements assign each contribution?",
            "status": "needs_counsel_confirmation",
        },
    ]
    counsel += [
        {"topic": "outside contributor", "question": o["counsel_question"], "status": o["status"]}
        for o in outside
    ]
    counsel += [
        {"topic": "disclosure", "question": d["counsel_question"], "status": d["status"]}
        for d in disclosures
        if d.get("counsel_question")
    ]
    return {
        "candidate": c,
        "title": note["title"],
        "note": nf,
        "search": lex["search"],
        "sources": {"contributors": contrib.get("file"), "disclosures": history.get("file")},
        "conception": {"date": conceived, **_q(nf, note["date_line"])},
        "problem": _q(nf, note["problem"]),
        "mechanism": steps,
        "elements": elements,
        "parameters": params,
        "alternatives": [_q(nf, a) for a in note["alternatives"]],
        "implementation": {"status": _implementation(note), **_q(nf, note["evidence"])},
        "missing_details": gaps,
        "missing_documents": missing_docs,
        "contributors": contributors,
        "agreements": agreements,
        "outside_contributions": outside,
        "disclosures": disclosures,
        "business_priority": (
            {"rank": rank["rank"], **_q(prio["file"], rank["title"])} if rank else {"rank": None}
        ),
        "counsel_questions": counsel,
    }


WEB_LIMIT = 10
SPAM = re.compile(
    r"!!!|\b(best deals?|buy now|click here|limited[- ]time|limited offer|guaranteed|"
    r"cheapest|lowest prices?|act now|in 24 hours)\b",
    re.I,
)
"""Words of an advert or a content farm: a result that holds them is not read."""


def web_query(groups: Sequence[Sequence[str]]) -> str:
    """The web query of a publication search: every term of every group, once each."""
    return " ".join(dict.fromkeys(str(t) for g in groups for t in g))


def choose_results(searches: Sequence[Mapping[str, Any]], known: set[str]) -> list[str]:
    """The web results worth reading, in rank order: no advert, no copy, no older version.

    A result whose title is one a publication already found has (among the inputs or an
    earlier result) is a copy of it on another site, and is not read again; of two results
    on one site with mostly the same title words, the older is an outdated version.
    """
    out: list[dict[str, Any]] = []
    titles = set(known)
    for data in searches:
        for h in data.get("results", ()):
            title = _norm(str(h.get("title", ""))).lower()
            if SPAM.search(f"{h.get('title', '')} {h.get('snippet', '')}") or title in titles:
                continue
            titles.add(title)
            if all(o["url"] != h["url"] for o in out):
                out.append({"url": str(h["url"]), "host": urlsplit(str(h["url"])).hostname,
                            "words": set(re.findall(r"[a-z0-9]+", title)) - {"the", "a", "of"},
                            "date": str(h.get("date") or "")})  # fmt: skip
    older = {
        a["url"]
        for a in out
        for b in out
        if a is not b and a["host"] == b["host"] and a["words"] and b["words"]
        and len(a["words"] & b["words"]) / len(a["words"] | b["words"]) >= 0.5
        and a["date"] and b["date"] and a["date"] < b["date"]
    }  # fmt: skip
    return [o["url"] for o in out if o["url"] not in older]


def matches(groups: Sequence[Sequence[str]], p: Mapping[str, Any]) -> bool:
    """Whether publication ``p``'s text matches every group (any one term of each)."""
    hay = " ".join([p.get("title") or "", p.get("abstract") or "", p.get("description") or "",
                    *(c["text"] for c in p.get("claims", ()))]).casefold()  # fmt: skip
    return all(any(str(t).casefold() in hay for t in g) for g in groups)


def prior_art(
    c: str, search: Mapping[str, Any], pubs: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Each publication recorded exactly, grouped into families by priority."""
    families = rules.family_of(pubs)
    by_id = {p["id"]: p for p in pubs}
    records = []
    for p in pubs:
        fam = next(k for k, v in families.items() if p["id"] in v)
        records.append(
            {
                "id": p["id"],
                "family": fam,
                "file": p["file"],
                "office": p["office"],
                "kind_code": p["kind_code"],
                "title": p["title"],
                "dates": p["dates"],
                "priority_claims": p["priority_claims"],
                "url": p["url"]
                if p["url"]
                else {"gap": {"question": f"What is the URL of {p['id']}?", "reason": "not_found"}},
                "status": [{**s, **_q(p["file"], s["wording"])} for s in p["status"]],
                "independent_claims": [
                    {"number": cl["number"], **_q(p["file"], cl["text"])}
                    for cl in p["claims"]
                    if cl["depends_on"] is None
                ],
                "dependent_claims": [
                    {
                        "number": cl["number"],
                        "depends_on": cl["depends_on"],
                        **_q(p["file"], cl["text"]),
                    }
                    for cl in p["claims"]
                    if cl["depends_on"] is not None
                ],
                "abstract": p["abstract"],
                "description": p["description"],
            }
        )
    fam_rows = []
    for fam, members in families.items():
        wordings = {m: [s["wording"] for s in by_id[m]["status"]] for m in members}
        claims = {
            m: [cl["text"] for cl in by_id[m]["claims"] if cl["depends_on"] is None]
            for m in members
        }
        diffs = []
        if len({tuple(v) for v in claims.values()}) > 1:
            diffs.append("independent claims differ between members")
        if len({tuple(v) for v in wordings.values()}) > 1 or any(
            len(v) > 1 for v in wordings.values()
        ):
            diffs.append("status wordings differ; each is kept as written")
        fam_rows.append(
            {"family": fam, "members": members, "status_wordings": wordings, "differences": diffs}
        )
    return {
        "candidate": c,
        "query": search["query"],
        "hits": [h["id"] for h in search["hits"]],
        "publications": records,
        "families": fam_rows,
    }


def comparison(c: str, ext: Mapping[str, Any], art: Mapping[str, Any]) -> dict[str, Any]:
    """The element-by-family matrix; ``not_found`` is never read as new."""
    pubs = {p["id"]: p for p in art["publications"]}

    def text(p: Mapping[str, Any]) -> str:
        claims = [cl["quote"] for cl in (*p["independent_claims"], *p["dependent_claims"])]
        return " ".join([p["title"] or "", p["abstract"], p["description"], *claims])

    def as_pub(p: Mapping[str, Any]) -> dict[str, Any]:
        claims = [
            {"number": cl["number"], "text": cl["quote"]}
            for cl in sorted(
                (*p["independent_claims"], *p["dependent_claims"]), key=lambda x: x["number"]
            )
        ]
        return {**p, "claims": claims}

    cells = []
    score: dict[str, int] = {}
    for element in ext["elements"]:
        eid, groups = element["id"], element["terms"]
        for fam in art["families"]:
            members = [pubs[m] for m in fam["members"]]
            state = _state(groups, " ".join(text(p) for p in members))
            per = {p["id"]: _state(groups, text(p)) for p in members}
            ev = []
            if state != "not_found":
                for p in members:
                    if per[p["id"]] != "not_found":
                        found = _evidence(as_pub(p), groups)
                        if found:
                            ev.append(found)
            cells.append(
                {
                    "element": eid,
                    "family": fam["family"],
                    "state": state,
                    "members": per,
                    "evidence": ev,
                }
            )
            score[fam["family"]] = (
                score.get(fam["family"], 0) + (state == "disclosed") * 2 + (state == "partial")
            )
    # The closest families are every family at the top score; a tie stays a tie, never
    # broken by an identifier's sort order.
    top = max(score.values(), default=0)
    closest = sorted(f for f, n in score.items() if n == top and n > 0)
    return {
        "candidate": c,
        "elements": [e["id"] for e in ext["elements"]],
        "families": [f["family"] for f in art["families"]],
        "cells": cells,
        "closest_families": closest,
        "note": "not_found means this search showed no reference for the element; it is "
        "not a finding that the element is new.",
    }


def recommendation(c: str, ext: tuple[str, Any], cmp_: tuple[str, Any]) -> dict[str, Any]:
    """The rule table's next action, and the model's explanation of it."""
    action, rule = rules.next_action(ext[1], cmp_[1])
    states = rules.element_states(cmp_[1])
    unseen = sorted(e for e, s in states.items() if s and all(x == "not_found" for x in s))
    shown: dict[str, list[str]] = {}
    for cell in cmp_[1]["cells"]:
        if cell["state"] == "disclosed":
            shown.setdefault(cell["family"], []).append(cell["element"])
    impl = ext[1]["implementation"]["status"]
    rank = ext[1]["business_priority"].get("rank")
    parts = [f"business priority {rank}", f"implementation: {impl}"]
    if unseen:
        parts.append(f"{', '.join(unseen)} not found in any family searched")
    for fam, els in sorted(shown.items()):
        parts.append(f"family {fam} discloses {', '.join(els)}")
    if ext[1]["missing_documents"]:
        parts.append(
            "referenced document missing: "
            + ", ".join(d["name"] for d in ext[1]["missing_documents"])
        )
    risks = []
    for d in ext[1]["disclosures"]:
        if d["public"] == "yes":
            risks.append(
                f"own public disclosure {d['disclosure_id']} on {d['date']} ({d['event_type']})"
            )
        elif d["date"] is None:
            risks.append(f"undated disclosure {d['disclosure_id']} ({d['event_type']})")
        elif d["public"] != "no":
            risks.append(f"disclosure {d['disclosure_id']} on {d['date']}: the record does not say "
                         "whether it was public")  # fmt: skip
    for o in ext[1]["outside_contributions"]:
        extra = (
            " and before the agreement's effective date" if o["before_agreement_effective"] else ""
        )
        risks.append(
            f"outside contributor {o['person_id']} suggested "
            f"{', '.join(o['elements'])} on {o['date']}, before the note{extra}"
        )
    change = {
        "R1": f"a reference discloses {', '.join(unseen)}, or an outside contributor's role "
        "changes inventorship or ownership",
        "R2": "the missing evidence appears and the unmatched elements stay not found",
        "R3": "counsel's view of the public disclosure, and an element no family shows",
        "R4": "an element the closest families lack proves material",
        "R5": "an element the closest family lacks proves material",
    }[rule]
    return {
        "candidate": c,
        "next_action": action,
        "rule": rule,
        "why": "; ".join(parts),
        "risks": risks,
        "would_change_if": change,
        "based_on": [ext[0], cmp_[0]],
    }


def _field(
    name: str, value: Any, status: str, source: str | None = None, **extra: Any
) -> dict[str, Any]:
    out: dict[str, Any] = {"field": name, "value": value, "status": status}
    if source:
        out["source"] = source
    out.update(extra)
    return out


def disclosure_fields(
    ext: Mapping[str, Any],
    cmp_: tuple[str, Any],
    art: tuple[str, Any],
    people: Mapping[str, Any],
    template: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """The invention disclosure form's fields; counsel fields say so."""
    nf = ext["note"]
    contributors = [
        {
            **{k: r[k] for k in ("person_id", "role", "contributed", "date")},
            "name": people.get(r["person_id"], {}).get("name"),
            "employer": people.get(r["person_id"], {}).get("employer"),
        }
        for r in ext["contributors"]
    ]
    outside = [
        {
            "person_id": a["person_id"],
            "agreement": a["ref"],
            "effective": a["effective"],
            "agreement_document": a["document"],
        }
        for a in ext["agreements"]
    ]
    outside += [
        {"person_id": r["person_id"], "role": r["role"]}
        for r in ext["contributors"]
        if OUTSIDE.search(str(r["agreement"]))
    ]
    questions = [q["question"] for q in ext["counsel_questions"]]
    gaps = [g["gap"]["question"] for g in ext["missing_details"]]
    params = [{k: p[k] for k in ("name", "value", "unit", "status")} for p in ext["parameters"]]
    public = [
        {
            k: d[k]
            for k in ("disclosure_id", "date", "elapsed_days", "event_type", "public", "status")
        }
        for d in ext["disclosures"]
        if d["own_disclosure"]
    ]
    oc = [o["counsel_question"] for o in ext["outside_contributions"]]
    return [
        _field("title", ext["title"], "complete", nf),
        _field("candidate_id", ext["candidate"], "complete", nf),
        _field("problem", ext["problem"]["quote"], "complete", nf),
        _field("mechanism_steps", [s["text"] for s in ext["mechanism"]], "complete", nf),
        _field("parameters", params, "complete", nf, open_details=gaps),
        _field(
            "alternatives_considered",
            [a["quote"] for a in ext["alternatives"]],
            "complete" if ext["alternatives"] else "missing",
            nf,
        ),
        _field(
            "difference_from_known_approaches",
            {"comparison": cmp_[0], "note": "not found is not new"},
            "complete",
            cmp_[0],
        ),
        _field(
            "contributors",
            contributors,
            "needs_counsel_confirmation",
            ext["sources"]["contributors"],
            counsel_question="Who are the inventors of each element?",
        ),
        _field(
            "conception_date_and_records",
            {"date": ext["conception"]["date"], "record": nf},
            "complete",
            nf,
        ),
        _field(
            "public_disclosures",
            public,
            "needs_counsel_confirmation"
            if any(d["status"] != "complete" for d in public)
            else "complete",
            ext["sources"]["disclosures"],
            **(
                {
                    "counsel_question": "; ".join(
                        d.get("counsel_question", "")
                        for d in ext["disclosures"]
                        if d.get("counsel_question")
                    )
                }
                if any(d["status"] != "complete" for d in public)
                else {}
            ),
        ),
        _field(
            "outside_resources",
            outside,
            "needs_counsel_confirmation" if outside or oc else "complete",
            ext["sources"]["contributors"],
            **(
                {
                    "counsel_question": " ".join(oc)
                    or "Do these agreements assign the contributions?"
                }
                if outside or oc
                else {}
            ),
        ),
        _field(
            "implementation_evidence",
            {
                "status": ext["implementation"]["status"],
                "evidence": ext["implementation"]["quote"],
                "missing_documents": [d["name"] for d in ext["missing_documents"]],
            },
            "complete",
            nf,
        ),
        _field(
            "figure_list",
            None,
            "needs_counsel_confirmation",
            None,
            counsel_question="The note has no drawings. Which figures should an application "
            "include?",
        ),
        _field("open_questions_for_counsel", questions + gaps, "complete", None),
    ]


def worksheet_fields(
    ext: Mapping[str, Any],
    cmp_: tuple[str, Any],
    people: Mapping[str, Any],
    template: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """The filing-information worksheet: facts and questions; counsel decides the rest."""
    inventors = sorted(
        {people.get(r["person_id"], {}).get("name") or r["person_id"] for r in ext["contributors"]}
    )
    q = "Counsel to confirm."
    return [
        _field("working_title", ext["title"], "complete", ext["note"]),
        _field("applicant", None, "needs_counsel_confirmation", counsel_question=q),
        _field(
            "proposed_inventors",
            inventors,
            "needs_counsel_confirmation",
            counsel_question="Who are the inventors?",
        ),
        _field(
            "ownership_and_assignments",
            [r["agreement"] for r in ext["contributors"]],
            "needs_counsel_confirmation",
            counsel_question=q,
        ),
        _field(
            "outside_party_agreements",
            [a["ref"] for a in ext["agreements"]],
            "needs_counsel_confirmation",
            counsel_question=q,
        ),
        _field(
            "earlier_public_disclosures",
            [d["disclosure_id"] for d in ext["disclosures"] if d["public"] != "no"],
            "needs_counsel_confirmation",
            counsel_question=q,
        ),
        _field("related_applications", None, "missing"),
        _field("priority_claims", None, "missing"),
        _field("jurisdictions_of_interest", None, "needs_counsel_confirmation", counsel_question=q),
        _field("representative", None, "missing"),
        _field("attachments", {"comparison": cmp_[0]}, "complete"),
    ]


FIELD_WORDS: Mapping[str, tuple[str, ...]] = {
    "title": ("title", "name"),
    "candidate_id": ("id", "identifier", "reference", "candidate"),
    "problem": ("problem", "background", "need"),
    "mechanism_steps": ("mechanism", "steps", "method", "how", "works", "description"),
    "parameters": ("parameters", "values", "settings"),
    "alternatives_considered": ("alternatives", "considered", "rejected", "options"),
    "difference_from_known_approaches": ("difference", "known", "prior", "art", "comparison"),
    "contributors": ("contributors", "inventors", "people", "contributions"),
    "conception_date_and_records": (
        "conception",
        "date",
        "records",
        "dated",
        "first",
        "written",
        "earliest",
    ),
    "public_disclosures": (
        "public",
        "disclosures",
        "disclosed",
        "shown",
        "showed",
        "demos",
        "talks",
        "shared",
    ),
    "outside_resources": ("outside", "resources", "third", "external", "funding", "grants"),
    "implementation_evidence": ("implementation", "evidence", "built", "tested", "prototype"),
    "figure_list": ("figure", "figures", "drawings"),
    "open_questions_for_counsel": ("questions", "counsel", "open"),
    "working_title": ("working", "title"),
    "applicant": ("applicant", "applicants"),
    "proposed_inventors": ("inventors", "proposed", "inventor"),
    "ownership_and_assignments": (
        "ownership",
        "assignment",
        "assignments",
        "owner",
        "owns",
        "owned",
    ),
    "outside_party_agreements": ("agreements", "party", "outside"),
    "earlier_public_disclosures": ("earlier", "public", "disclosures"),
    "related_applications": ("related", "applications"),
    "priority_claims": ("priority", "claims"),
    "jurisdictions_of_interest": ("jurisdictions", "countries", "territories"),
    "representative": ("representative", "attorney", "agent"),
    "attachments": ("attachments", "annex", "enclosures"),
}
"""The words a template's field may use for each value the model prepares."""


_FILLER = frozenset({"and", "of", "the", "for", "with", "it", "a", "an", "to", "in"})


def _tokens(name: str) -> set[str]:
    """A field name's words, each with its singular (``records`` also counts as ``record``)."""
    words = {t for t in re.split(r"[^a-z0-9]+", name.lower()) if t and t not in _FILLER}
    return words | {w[:-1] for w in words if len(w) > 3 and w.endswith("s")}


def fit(template: Mapping[str, Any], fields: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The prepared values laid onto ``template``'s own fields (any template's names).

    A template field takes the prepared value of the same name, else the one whose words
    it shares most (:data:`FIELD_WORDS`); one it shares none with stays ``missing``, or
    ``needs_counsel_confirmation`` when the template says counsel decides it. A field the
    template reserves for counsel is never marked complete.
    """
    by_name = {str(f["field"]): f for f in fields}
    out: list[dict[str, Any]] = []
    for decl in template["fields"]:
        name = str(decl["field"])
        src = by_name.get(name)
        if src is None:
            want = _tokens(name)
            scored = [
                (len(want & (_tokens(k) | _tokens(" ".join(FIELD_WORDS.get(k, ()))))), -i, f)
                for i, (k, f) in enumerate(by_name.items())
            ]
            best = max(scored, key=lambda x: (x[0], x[1]), default=None)
            src = best[2] if best is not None and best[0] > 0 else None
        default = str(decl.get("default_status") or "")
        counsel = default.startswith("needs_counsel_confirmation")
        if src is None:
            status = "needs_counsel_confirmation" if counsel else "missing"
            f: dict[str, Any] = {"field": name, "value": None, "status": status}
        else:
            f = {**src, "field": name}
            if counsel and f.get("status") == "complete":
                f["status"] = "needs_counsel_confirmation"
        if f["status"] == "needs_counsel_confirmation" and not f.get("counsel_question"):
            f["counsel_question"] = f"Counsel to confirm the {name.replace('_', ' ')}."
        if f["status"] == "complete" and f.get("value") in (None, "", [], {}):
            f["status"] = "missing"
        out.append(f)
    return out


def packet(
    c: str, reads: Mapping[str, tuple[str, Any]], forms: Sequence[tuple[str, Any]]
) -> dict[str, Any]:
    """The counsel-review packet: the counsel section first, then the scope note and parts."""
    ext = reads["extraction"][1]
    counsel_items = [
        {"form": ref, "field": f["field"], "question": f.get("counsel_question", "")}
        for ref, content in forms
        for f in content["fields"]
        if f["status"] == "needs_counsel_confirmation"
    ]
    missing = [
        {"form": ref, "field": f["field"]}
        for ref, content in forms
        for f in content["fields"]
        if f["status"] == "missing"
    ]
    counts = {s: sum(content["counts"][s] for _, content in forms) for s in rules.FIELD_STATUSES}
    cmp_ = reads["comparison"][1]
    rec = reads["recommendation"][1]
    return {
        "candidate": c,
        "scope_note": SCOPE_NOTE,
        "sections": [
            {
                "title": "Needs counsel confirmation",
                "items": counsel_items,
                "questions": [q["question"] for q in ext["counsel_questions"]],
            },
            {"title": "Scope", "text": SCOPE_NOTE},
            {
                "title": "Forms",
                "forms": [ref for ref, _ in forms],
                "counts": counts,
                "missing": missing,
            },
            {
                "title": "Prior-art matrix",
                "comparison": reads["comparison"][0],
                "families": cmp_["families"],
                "closest_families": cmp_["closest_families"],
                "cells": [{k: x[k] for k in ("element", "family", "state")} for x in cmp_["cells"]],
            },
            {
                "title": "Recommendation",
                "recommendation": reads["recommendation"][0],
                "next_action": rec["next_action"],
                "why": rec["why"],
                "risks": rec["risks"],
            },
            {
                "title": "Contributors",
                "outside_contributions": ext["outside_contributions"],
                "contributors": [
                    {k: r[k] for k in ("person_id", "role", "contributed")}
                    for r in ext["contributors"]
                ],
            },
        ],
        "inputs": {
            **{k: v[0] for k, v in sorted(reads.items())},
            "forms": [ref for ref, _ in forms],
        },
    }


ADAPTERS = {
    "generate": PatentFake(),
    **{k: v for k, v in fakes_views.READERS.items() if k not in ("table_roles", "candidate_of")},
}
"""The ``fake`` run mode's model per alias: the composition root binds it (ADR-P07)."""
