"""The ``fake`` run mode's generation model for the patent agent: rules, no network.

``PatentFake`` reads only what its request carries (the read block, the tool results of
this instance's turns, the person's answers, a rebuild notice) and answers with tool
calls. It stands in for a live model so a run is offline and repeatable; it is not a
claim that rules can do this work in general.

The model-side knowledge is ``LEXICON``: per candidate, the elements a skilled reader
would pick out of the engineering note, each with the terms that find its mechanism step
and the term groups that find it in a reference (all groups: ``disclosed``; at least half:
``partial``; fewer: ``not_found``). It was written by reading the source texts, never the
answer key, and a live model replaces it. Everything that is a rule rather than a reading
(the next action, family grouping, form statuses) lives in ``rules.py`` and is checked in
code, so this model cannot override it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aksha.runtime.api import GenerationRequest, GenerationResult, RuleGenerationModel, ToolCall

from . import rules

Groups = list[list[str]]

LEXICON: Mapping[str, Mapping[str, Any]] = {
    "C1": {
        "search": [["background"], ["sensor"]],
        "elements": {
            "F1": (
                "per-pixel thermopile empty-room baseline",
                ["thermopile array"],
                [
                    ["thermal array", "thermopile", "thermal occupancy sensor"],
                    ["background frame", "background estimate", "baseline"],
                    ["pixel"],
                ],
            ),
            "F2": (
                "baseline update only after a PIR quiet window",
                ["no motion for 20 minutes"],
                [
                    ["background"],
                    ["no motion", "low activity"],
                    ["quiet period", "holding period", "no motion has been detected"],
                ],
            ),
            "F3": (
                "baseline update also gated on the door contact reporting closed",
                ["door contact reports closed"],
                [["door"]],
            ),
            "F4": (
                "slow EMA update with per-pixel freeze after detection",
                ["Freeze learning"],
                [
                    ["running average", "moving average", "stationary occupant is not absorbed"],
                    ["freeze", "update weight"],
                ],
            ),
            "F5": (
                "per-pixel threshold from that pixel's noise",
                ["detection threshold"],
                [["threshold derived from", "noise estimate of that pixel", "noise"]],
            ),
        },
        "public_terms": {"F2": ["no motion"], "F3": ["door"]},
    },
    "C2": {
        "search": [["mount point", "placement"]],
        "elements": {
            "P1": (
                "footprint at seated head height from field of view",
                ["footprint"],
                [["footprint"], ["working height", "head height"], ["field of view"]],
            ),
            "P2": (
                "layout score over seat coverage, overlap, heat-source and doorway exposure",
                ["Score each layout"],
                [
                    ["coverage of seat", "seat locations", "seats"],
                    ["doorway"],
                    ["heat source", "vent"],
                ],
            ),
            "P3": (
                "post-install walk test against the plan",
                ["walk one person"],
                [["walk test", "walk"]],
            ),
        },
    },
    "C3": {
        "search": [["overlap"]],
        "elements": {
            "Q1": (
                "on-device processing, counts only",
                ["outputs counts only"],
                [
                    ["locally", "on the device", "on-device"],
                    ["transmitting only the counts", "transmit only", "transmits only"],
                ],
            ),
            "Q2": (
                "overlap zone marked in each unit's pixel coordinates at install",
                ["overlap zone is marked"],
                [["overlap"], ["pixel coordinates"]],
            ),
            "Q3": (
                "each unit reports total and overlap counts",
                ["reports two integers"],
                [
                    ["count of persons inside", "counting persons within the overlap region"],
                    ["transmits only", "transmitting only"],
                ],
            ),
            "Q4": (
                "room = A + B - min(overlap_A, overlap_B)",
                ["min(overlap_A"],
                [["counted once", "minus the overlap count"], ["min(", "minimum of"]],
            ),
            "Q5": (
                "single-occupant rooms grouped in roll-ups",
                ["roll up to floors"],
                [["floor level", "roll up"], ["grouped", "single regular occupant"]],
            ),
        },
    },
    "C4": {
        "search": [["passive infrared", "PIR"], ["battery"]],
        "elements": {
            "B1": (
                "PIR wakes the thermopile for a 60 s window at 1 Hz",
                ["When the PIR fires"],
                [["passive infrared"], ["wakes"], ["window"]],
            ),
            "B2": (
                "fall back to 10 s sampling then PIR-only",
                ["drop back"],
                [["10 s", "every 10 seconds", "reduced rate", "fallback"], ["switched off"]],
            ),
            "B3": (
                "report on change plus 15 min heartbeat",
                ["heartbeat every 15 minutes"],
                [
                    ["only when the occupant count changes", "only when an occupant count changes"],
                    ["heartbeat"],
                ],
            ),
            "B4": (
                "estimated 0.2 mA average (estimate, not measured)",
                ["Average current"],
                [["0.2 mA", "average current"]],
            ),
        },
    },
}

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
        elif msg.role == "user" and text.startswith("Rebuild: your earlier version"):
            body = text.split("\n", 2)[1]
            try:
                ctx.rebuild_old = json.loads(body)
            except ValueError:
                ctx.rebuild_old = None
        for c in msg.tool_calls:
            calls[c.call_id] = (c.name, dict(c.arguments))
        if msg.role == "tool" and msg.call_id in calls:
            name, args = calls[msg.call_id]
            try:
                result = json.loads(text)
            except ValueError:
                result = {"status": "error"}
            ctx.results.append((name, args, result))
            if name == "finish":
                # Asked again after a finish: the work was re-entered (or the finish was
                # refused), so what was read before may be out of date. Start over.
                ctx.results.clear()
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


class PatentFake(RuleGenerationModel):
    """The rule-based generation model (module doc). One method per Task."""

    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Answer from the Task's rule, over what the request carries."""
        ctx = parse(request)
        handler = getattr(self, f"_{ctx.task}", None)
        if handler is None:
            return _reply(_call("finish", 1, summary="nothing for this Task"))
        result: GenerationResult = handler(ctx)
        return result

    # --- portfolio ----------------------------------------------------------------------

    def _scope(self, ctx: Context) -> GenerationResult:
        listed = ctx.ok("list_candidates")
        if not listed:
            return _reply(_call("list_candidates", 1))
        rows = listed[-1][1]["candidates"]
        content = {
            "candidates": [r["candidate"] for r in rows],
            "notes": [
                {"candidate": r["candidate"], "file": r["note"], "title": r["title"]} for r in rows
            ],
        }
        return _reply(
            _call("save_artifact", 1, kind="portfolio", content=content),
            _call("finish", 1, summary="four candidates listed"),
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
        done = {a["path"] for a, _ in ctx.ok("read_document")}
        todo = [p for p in linked if p not in done]
        if todo and not ctx.called("read_document"):
            return _reply(*(_call("read_document", i, path=p) for i, p in enumerate(todo, 1)))
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
        if not ctx.called("search_publications"):
            return _reply(_call("search_publications", 1, all_of=LEXICON[c]["search"]))
        search = ctx.ok("search_publications")[-1][1]
        if not ctx.called("fetch_publication"):
            return _reply(
                *(
                    _call("fetch_publication", i, id=h["id"])
                    for i, h in enumerate(search["hits"], 1)
                )
            )
        pubs = [d for _, d in ctx.ok("fetch_publication")]
        content = prior_art(c, search, pubs)
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
        if not ctx.called("read_template"):
            ids = sorted({r["person_id"] for r in ext[1]["contributors"]})
            return _reply(
                _call("read_people", 1, person_ids=ids),
                _call("read_template", 1, template="invention-disclosure-form"),
                _call("read_template", 2, template="filing-information-worksheet"),
            )
        people = {p["person_id"]: p for _, d in ctx.ok("read_people") for p in d["people"]}
        templates = {a["template"]: d for a, d in ctx.ok("read_template")}
        cmp_, rec, art = ctx.read("comparison"), ctx.read("recommendation"), ctx.read("prior_art")
        assert cmp_ is not None and rec is not None and art is not None
        form = disclosure_fields(ext[1], cmp_, art, people, templates["invention-disclosure-form"])
        ws = worksheet_fields(ext[1], cmp_, people, templates["filing-information-worksheet"])
        return _reply(
            _call("fill_form", 1, template="invention-disclosure-form", candidate=c, fields=form),
            _call("fill_form", 2, template="filing-information-worksheet", candidate=c, fields=ws),
            _call("finish", 1, summary="forms filled"),
        )

    def _packet(self, ctx: Context) -> GenerationResult:
        c = str(ctx.key)
        reads = {
            k: ctx.read(k)
            for k in (
                "disclosure",
                "worksheet",
                "comparison",
                "recommendation",
                "extraction",
                "prior_art",
            )
        }
        assert all(v is not None for v in reads.values()), reads.keys()
        content = packet(c, {k: v for k, v in reads.items() if v is not None})
        return _reply(
            _call("save_artifact", 1, kind="packet", key=c, content=content),
            _call("finish", 1, summary="packet ready for review"),
        )

    def _send(self, ctx: Context) -> GenerationResult:
        c = str(ctx.key)
        pkt = ctx.read("packet")
        assert pkt is not None
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


def _q(file: str, quote: str, **extra: Any) -> dict[str, Any]:
    return {"file": file, "quote": quote, **extra}


def _implementation(note: Mapping[str, Any]) -> str:
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
    docs = {a["path"]: d for a, d in ctx.ok("read_document")}
    nf = note["file"]
    lex = LEXICON[c]
    steps = [{"n": s["n"], "text": s["text"], **_q(nf, s["text"])} for s in note["mechanism_steps"]]
    elements = []
    for eid, (label, step_terms, _groups) in lex["elements"].items():
        step = next((s for s in steps if _find(s["text"], step_terms)), None)
        elements.append(
            {
                "id": eid,
                "label": label,
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
                        "agreement_ref",
                    )
                },
                "elements": hit,
                **_q(contrib["file"], r["contributed"]),
            }
        )
        m = re.search(r"(CA-\d{4}-\d+), effective (\d{4}-\d{2}-\d{2})", r["agreement_ref"])
        if m:
            found = any(d["found"] for d in r["agreement_documents"] if d["ref"] == m.group(1))
            agreements.append(
                {
                    "ref": m.group(1),
                    "effective": m.group(2),
                    "person_id": r["person_id"],
                    "document": "found" if found else "referenced_not_found",
                    **_q(contrib["file"], r["agreement_ref"]),
                }
            )
    disclosures, outside = [], []
    conceived = note["date"]
    for r in history["rows"]:
        row = {
            "event_id": r["event_id"],
            "date": r["date"],
            "elapsed_days": r.get("elapsed_days"),
            "as_of": history.get("as_of"),
            "event_type": r["event_type"],
            "public": r["public"],
            "own_disclosure": r["event_type"] != "outside email",
            "describes_fully": r["what_was_shown"].startswith("Full description"),
            **_q(history["file"], r["what_was_shown"]),
            "row": _q(history["file"], r["row_start"]),
        }
        if r["date"] is None or r["public"] not in ("yes", "no"):
            row["status"] = "needs_counsel_confirmation"
            row["counsel_question"] = (
                f"{r['event_id']}: when was it sent, and to how many "
                "people? The record has no date."
            )
        elif r["public"] == "yes":
            row["status"] = "needs_counsel_confirmation"
            row["counsel_question"] = (
                f"{r['event_id']} was public {r.get('elapsed_days')} days "
                "before today. Does any grace period apply?"
            )
        else:
            row["status"] = "complete"
        doc = docs.get(r["linked_file"] or "")
        if doc and doc["type"] == "record":
            shown_text = doc["sections"].get("what", "")
            shown = [
                e
                for e, terms in lex.get("public_terms", {}).items()
                if _clause(shown_text, terms[:1])
            ]
            clause = _clause(shown_text, [t[0] for t in lex.get("public_terms", {}).values()])
            row["elements_shown"] = shown
            if clause:
                row["shown_quote"] = _q(doc["file"], clause)
        if r["event_type"] == "outside email" and doc and doc["type"] == "email":
            sentences = re.findall(r"[^.?!]*[.?!]", doc["body"])
            suggestion = next(
                (s.strip() for s in sentences if "door contact" in s and s.strip().endswith("?")),
                None,
            )
            hit = [
                e
                for e, terms in lex.get("public_terms", {}).items()
                if suggestion and _find(suggestion, ["door", "quiet"])
            ]
            agreement = next((a for a in agreements if a["person_id"] == r["by"]), None)
            before_agreement = (
                (doc["date"] < agreement["effective"]) if agreement and doc["date"] else None
            )
            q = (
                "An outside consultant suggested the door-contact signal before the internal "
                "note. Who are the inventors, and does the consulting agreement affect "
                "ownership?"
            )
            if before_agreement:
                q = (
                    "An outside consultant suggested the door-contact signal before the internal "
                    "note and before the consulting agreement's effective date. Who are the "
                    "inventors, and who owns a suggestion made outside the agreement?"
                )
            outside.append(
                {
                    "person_id": r["by"],
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
    for eid, (_label, _terms, groups) in LEXICON[c]["elements"].items():
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
                f"own public disclosure {d['event_id']} on {d['date']} ({d['event_type']})"
            )
        elif d["date"] is None:
            risks.append(f"undated disclosure {d['event_id']} ({d['event_type']})")
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
        if "contractor" in r["agreement_ref"]
    ]
    questions = [q["question"] for q in ext["counsel_questions"]]
    gaps = [g["gap"]["question"] for g in ext["missing_details"]]
    params = [{k: p[k] for k in ("name", "value", "unit", "status")} for p in ext["parameters"]]
    public = [
        {k: d[k] for k in ("event_id", "date", "elapsed_days", "event_type", "public", "status")}
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
            "contributors/records.csv",
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
            "disclosures/history.csv",
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
            "contributors/records.csv",
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
            [r["agreement_ref"] for r in ext["contributors"]],
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
            [d["event_id"] for d in ext["disclosures"] if d["public"] != "no"],
            "needs_counsel_confirmation",
            counsel_question=q,
        ),
        _field("related_applications", None, "missing"),
        _field("priority_claims", None, "missing"),
        _field("jurisdictions_of_interest", None, "needs_counsel_confirmation", counsel_question=q),
        _field("representative", None, "missing"),
        _field("attachments", {"comparison": cmp_[0]}, "complete"),
    ]


def packet(c: str, reads: Mapping[str, tuple[str, Any]]) -> dict[str, Any]:
    """The counsel-review packet: the counsel section first, then the scope note and parts."""
    ext = reads["extraction"][1]
    forms = [reads["disclosure"], reads["worksheet"]]
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
                "disclosure": reads["disclosure"][0],
                "worksheet": reads["worksheet"][0],
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
        "inputs": {k: v[0] for k, v in sorted(reads.items())},
    }


ADAPTERS = {"generate": PatentFake()}
"""The ``fake`` run mode's model per alias: the composition root binds it (ADR-P07)."""
