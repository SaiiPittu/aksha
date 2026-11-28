"""The patent agent's eval adapter: its runtime run, saved into the episode's bundle schema.

The agent runs as it ships (``aksha.agents.patent``, its rule-based fake model, its own
tools reading through :class:`~aksha.evals.agents.runtime.EpisodeReader`). The scripted user
answers the inventor's gap question with the answer ``make demo`` gives, chooses the
candidate the agent's table puts first for counsel review, and approves the packet send;
the send goes through the outbox to :class:`~aksha.evals.agents.runtime.EpisodeMailbox`,
bound to the episode approval for ``packet/<C>``.

=============================  =============================================================
Bundle output                  From the run's artifacts
=============================  =============================================================
``extraction/<C>``             ``extraction`` as ``entries``, each named by the key's fact
                               kind: ``mechanism_step`` (one per element, its ``element``
                               label), ``parameter`` (each, its quantity), ``conception_record``
                               (the record's date, and an element's own when a later entry
                               dates it), ``alternative_rejected``, ``missing_detail`` (an
                               open point: null, a question), ``missing_document``, ``opinion``,
                               and ``inventorship`` and ``ownership`` (the counsel questions,
                               status ``needs_counsel_confirmation``)
``missing_documents/<C>``      ``extraction.missing_documents``
``disclosures/<C>``            ``extraction.disclosures``: own and third-party events, a
                               null date kept null, the elements each shows
``matrix/<C>``                 ``comparison.cells``: element to family to state, each
                               family named by its priority number, citing the evidence
``recommendation/<C>``         ``recommendation``: next action and why
``priorities/all``             each extraction's ``business_priority`` rank
``family/<F>``                 ``prior_art.families``: members and differences
``family_status/<F>``          each member's status wordings, verbatim
``family_member_of``           each later member to the first, cited by the priority number
``packet/<C>``                 ``packet``, saved before the approval is asked for
=============================  =============================================================

A quote's offsets come from the text the episode returned for its file, the first place it
occurs. The adapter computes nothing a grader checks: it renames fields and adds offsets.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from aksha.agents.patent.agent import tasks as patent_tasks
from aksha.agents.patent.portfolio import SPEC as PORTFOLIO
from aksha.evals.agents.runtime import EpisodeMailbox, EpisodeReader, RuntimeUnderTest
from aksha.evals.episode import Episode
from aksha.extensions.portfolio import goal, items, review, supervised
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.versions import current
from aksha.runtime.projections.view import LogView
from aksha.runtime.session.views import Waiting
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tasks.types import InstanceRef
from aksha.runtime.tasks.view import TaskView

AGENT = "patent"
SOURCE = "inputs"
MAIL = "counsel_mail"
SEND_ACTION = "send_counsel_packet"
GAP_ANSWERS: Mapping[str, str] = {
    "stillroom": "0.5 m from the vent edge, so its air flow stays outside the footprint.",
    "stillroom-v2": "0.5 m from the vent edge, so its air flow stays outside the footprint.",
    "tilldew": "The state utility's published time-of-day tariff table.",
    "cindral": "45 minutes by public transport.",
}
"""The inventor's answer to a note's open detail, per world (the scripted user's knowledge)."""
GAP_DEFAULT = "Not decided yet; the note's author will confirm it."
FIRST_FOR_COUNSEL = "prioritize_counsel_review"
COUNSEL = "needs_counsel_confirmation"


class PatentUnderTest(RuntimeUnderTest):
    """The patent agent through an episode (module doc)."""

    name = AGENT
    agent = AGENT
    sources = (SOURCE,)
    systems = (MAIL,)
    request = "Assess the invention candidates and prepare the chosen one for counsel."
    world = ""

    def run(self, episode: Episode) -> None:
        """Remember the episode's world (the scripted user's answers depend on it), then run."""
        self.world = episode.world
        super().run(episode)

    def answer(self, w: Waiting, question: str, text: str) -> str:
        """The gap answer, or the candidate the table puts first for counsel review."""
        if w.kind == "choice":
            # The options are the table's candidates, each labelled with its next action by
            # code (never parsed out of the model's question).
            labelled = list(zip(w.options, w.option_labels, strict=False))
            first = [c for c, label in labelled if label.startswith(f"{FIRST_FOR_COUNSEL}:")]
            return str((first or list(w.options) or ["none"])[0])
        if w.kind == "input":
            return GAP_ANSWERS.get(self.world, GAP_DEFAULT)
        return super().answer(w, question, text)

    def request_approval(
        self, view: LogView, episode: Episode, boxes: Mapping[str, EpisodeMailbox], w: Waiting
    ) -> bool:
        """Ask for ``packet/<C>``'s send to the recipient the approval names; bind it."""
        facts = dict(w.facts)
        packet, recipient = facts.get("packet", ""), facts.get("recipient", "")
        cand = packet.partition("/")[2].partition("@")[0]
        if w.tool != "send_packet" or not cand or not recipient or MAIL not in boxes:
            return False
        approval = episode.request_approval(f"packet/{cand}", SEND_ACTION, recipient)
        if approval is None:
            return False
        boxes[MAIL].grants[packet] = (approval, SEND_ACTION, recipient, f"packet/{cand}")
        return True

    def export(
        self, view: LogView, episode: Episode, reader: EpisodeReader, blobs: BlobStore | None
    ) -> None:
        """Every candidate's outputs, the families, the priorities and the packet."""

        def content(kind: str, key: str | None) -> Mapping[str, Any]:
            found = current(view, f"{AGENT}.{kind}", key, blobs=blobs)
            return {} if found is None else found.content

        candidates = [str(c) for c in content("portfolio", None).get("candidates", ())]
        ranks: dict[str, Any] = {}
        rank_cites: list[dict[str, Any]] = []
        families: dict[str, Mapping[str, Any]] = {}
        publications: dict[str, Mapping[str, Any]] = {}
        for cand in candidates:
            ext = content("extraction", cand)
            if ext:
                self._extraction(episode, reader, cand, ext)
                prio = ext.get("business_priority")
                if isinstance(prio, Mapping) and prio.get("rank") is not None:
                    ranks[cand] = prio.get("rank")
                    rank_cites.extend(_cites(reader, [prio]))
            comp = content("comparison", cand)
            if comp:
                cells: dict[str, dict[str, str]] = {}
                evidence: list[Any] = []
                for cell in comp.get("cells", ()):
                    row = cells.setdefault(str(cell.get("element")), {})
                    row[str(cell.get("family"))] = str(cell.get("state"))
                    evidence.extend(e for e in cell.get("evidence", ()) if isinstance(e, Mapping))
                episode.save("matrix", cand, {"cells": cells}, _cites(reader, evidence))
            rec = content("recommendation", cand)
            if rec:
                chosen = {"next_action": rec.get("next_action"), "why": rec.get("why")}
                episode.save("recommendation", cand, chosen)
            art = content("prior_art", cand)
            for fam in art.get("families", ()):
                families.setdefault(str(fam.get("family")), fam)
            for pub in art.get("publications", ()):
                publications.setdefault(str(pub.get("id")), pub)
        if ranks:
            episode.save("priorities", "all", {"ranks": ranks}, rank_cites)
        for name, fam in sorted(families.items()):
            self._family(episode, reader, name, fam, publications)
        for cand in candidates:
            packet = content("packet", cand)
            if packet:
                episode.save("packet", cand, packet)
        _portfolio(view, episode)

    def _extraction(
        self, ep: Episode, reader: EpisodeReader, cand: str, ext: Mapping[str, Any]
    ) -> None:
        out: list[dict[str, Any]] = []

        def add(kind: str, value: Any, items: Iterable[object], **extra: Any) -> None:
            status = extra.pop("status", None) or ("known" if value is not None else "missing")
            out.append({"kind": kind, **extra, "value": value, "status": status,
                        "question": extra.pop("question", None),
                        "citations": _cites(reader, items)})  # fmt: skip

        for e in ext.get("elements", ()):
            if isinstance(e, Mapping):
                add("mechanism_step", e.get("quote") or e.get("label"), [e], element=e.get("id"))
                conceived = e.get("conceived")
                if isinstance(conceived, Mapping) and conceived.get("date"):
                    add("conception_record", conceived.get("date"), [conceived],
                        element=e.get("id"))  # fmt: skip
        for p in ext.get("parameters", ()):
            if not isinstance(p, Mapping):
                continue
            value = (
                None
                if p.get("value") is None
                else {
                    "name": p.get("name"),
                    "value": _number(p.get("value")),
                    "unit": p.get("unit"),
                }
            )
            status = {"complete": "known"}.get(str(p.get("status")), p.get("status"))
            add("parameter", value, [p], status=status, question=p.get("question"))
        conception = ext.get("conception")
        if isinstance(conception, Mapping) and conception.get("date"):
            add("conception_record", conception.get("date"), [conception])
        for a in ext.get("alternatives", ()):
            if isinstance(a, Mapping):
                add("alternative_rejected", a.get("quote"), [a])
        for gap in ext.get("missing_details", ()):
            if isinstance(gap, Mapping) and gap.get("value") is None:
                question = dict(gap.get("gap") or {}).get("question")
                add("missing_detail", None, [gap], status="missing", question=question)
        for o in ext.get("opinions", ()):
            if isinstance(o, Mapping):
                add("opinion", o.get("quote"), [o], status="opinion")
        people = [*ext.get("outside_contributions", ()), *ext.get("contributors", ()),
                  *ext.get("agreements", ()), *ext.get("people_terms", ())]  # fmt: skip
        for topic in ("inventorship", "ownership"):
            q = next((c for c in ext.get("counsel_questions", ()) if c.get("topic") == topic), None)
            if q is not None:
                add(
                    topic, None, people, status=q.get("status", COUNSEL), question=q.get("question")
                )
        missing = [m for m in ext.get("missing_documents", ()) if isinstance(m, Mapping)]
        for m in missing:
            add("missing_document", None, [m], status="not_found")
        if missing:
            ep.save("missing_documents", cand, {"not_found": [m.get("name") for m in missing]},
                    _cites(reader, missing))  # fmt: skip
        ep.save("extraction", cand, {"entries": out})
        events = [
            {
                "id": d.get("disclosure_id"),
                "date": d.get("date"),
                "public": d.get("public") == "yes",
                "own": bool(d.get("own_disclosure")),
                "status": d.get("status"),
                "cited_as_prior_art": False,
                "citation": _cite(reader, d),
                "elements": list(d.get("elements_shown", ())),
                "evidence": _cites(
                    reader, [d.get("shown_quote"), d.get("row"), *d.get("shown_quotes", ())]
                ),
            }
            for d in ext.get("disclosures", ())
            if isinstance(d, Mapping)
        ]
        if events:
            ep.save("disclosures", cand, {"events": events})

    def _family(
        self,
        ep: Episode,
        reader: EpisodeReader,
        name: str,
        fam: Mapping[str, Any],
        publications: Mapping[str, Mapping[str, Any]],
    ) -> None:
        members = [str(m) for m in fam.get("members", ())]
        differences = list(fam.get("differences", ()))
        ep.save("family", name, {"members": members, "material_differences": differences})
        statuses = [
            {"member": m, "wording": str(wording)}
            for m, wordings in dict(fam.get("status_wordings", {})).items()
            for wording in wordings
        ]
        cites = _cites(
            reader, [s for m in members for s in publications.get(m, {}).get("status", ())]
        )
        if statuses:
            ep.save("family_status", name, {"statuses": statuses}, cites)
        for member in members[1:]:
            pub = publications.get(member, {})
            file = str(pub.get("file", ""))
            excerpt = reader.cite(file, name) if file else None
            ep.relate("family_member_of", f"pub:{member}", f"pub:{members[0]}", excerpt)


def _known(value: Any, citations: list[dict[str, Any]]) -> dict[str, Any]:
    return {"value": value, "status": "known", "question": None, "citations": citations}


def _cite(reader: EpisodeReader, item: object) -> dict[str, Any] | None:
    """The citation of an artifact item's ``{file, quote}``; None when it quotes nothing."""
    if not isinstance(item, Mapping) or not item.get("file") or not item.get("quote"):
        return None
    return reader.cite(str(item["file"]), str(item["quote"]))


def _cites(reader: EpisodeReader, items: Iterable[object]) -> list[dict[str, Any]]:
    """The citations of every item that quotes something."""
    return [c for c in (_cite(reader, i) for i in items) if c is not None]


def _number(value: Any) -> Any:
    text = str(value)
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return value  # a decimal stays as written: the store refuses floats


def _tier(instance: str) -> str:
    """Which part of the portfolio a model step's instance belongs to."""
    head, _, rest = instance.partition("/")
    if head == PORTFOLIO.supervisor:
        return "supervisor"
    if head.startswith(f"{PORTFOLIO.supervisor}["):
        return "deep" if rest.startswith(f"{PORTFOLIO.deep.task}") else "triage"
    return "other"


def _instance_of(step: str) -> str:
    parts = step.split("/")[1:]
    out = []
    for p in parts:
        if re.fullmatch(r"t\d+", p):
            break
        out.append(p)
    return "/".join(out)


def _portfolio(view: LogView, episode: Episode) -> None:
    """The supervisor's outputs, when its candidates ran under it (ADR-P15 D7 S5).

    Counted from the runtime's own facts, as :func:`run_metrics` counts: ``review`` is
    the kit's batched review; ``supervisor`` what the supervisor's model steps were shown
    (``step.started{rendered, context_chars}``) and which tools it called; ``cost`` the
    model turns by tier and the items each tier ran for; ``goal`` every evaluation of the
    supervisor's completion check, the goal as it stood at each supervisor turn (what the
    board's GOAL line told it), and where its DONE and the first selection Task fell.
    """
    run = next((e.run for e in view.events if e.type == "run.started"
                and e.payload.get("cause") == "request" and e.run), None)  # fmt: skip
    if run is None:
        return
    tree = compile_tree(AGENT, patent_tasks())
    lead = InstanceRef(run, PORTFOLIO.supervisor)
    tv = TaskView(view, tree, lead)
    if not supervised(tv, PORTFOLIO):
        return
    episode.save("portfolio", "review", review(tv, PORTFOLIO))
    turns = {"supervisor": 0, "triage": 0, "deep": 0, "other": 0}
    keys: dict[str, set[str]] = {"triage": set(), "deep": set()}
    rendered: list[str] = []
    chars: list[int] = []
    tools: dict[str, int] = {}
    for e in view.events:
        if e.run != run or not e.step:
            continue
        instance = _instance_of(e.step)
        tier = _tier(instance)
        p = e.payload
        if e.type == "step.completed" and p.get("kind") == "model":
            turns[tier] += 1
            if tier in keys:
                keys[tier].add(InstanceRef.parse(run, instance).key or "")
        if tier != "supervisor":
            continue
        if e.type == "step.started" and p.get("kind") == "model":
            rendered += [str(r.get("ref")) for r in p.get("rendered", ())]
            chars.append(int(p.get("context_chars", 0)))
        elif e.type == "step.completed" and p.get("kind") == "tool":
            name = str(p.get("tool") or p.get("name"))
            tools[name] = tools.get(name, 0) + 1
    episode.save("portfolio", "supervisor", {
        "turns": turns["supervisor"], "rendered": rendered, "tool_calls": tools,
        "max_context_chars": max(chars, default=0),
        "tools_offered": sorted(tree.node(PORTFOLIO.supervisor).task.tools),
    })  # fmt: skip
    episode.save("portfolio", "cost", {
        "model_turns": turns, "items": len(items(tv, PORTFOLIO) or ()),
        "triage_items": len(keys["triage"]), "deep_items": len(keys["deep"]),
    })  # fmt: skip
    check = goal(PORTFOLIO)
    turns_read = []
    for e in view.events:
        if (e.type == "stop.decided" and e.run == run and e.instance == PORTFOLIO.supervisor
                and e.payload.get("verdict") == "continue"):  # fmt: skip
            seen = check(TaskView(view.upto(int(e.payload.get("seq_read", e.seq - 1))), tree, lead))
            turns_read.append({"seq": e.seq, "reason": e.payload.get("reason"), "goal_ok": seen.ok,
                               "goal": seen.message, "clears": seen.clears})  # fmt: skip
    evaluations = [
        {"seq": e.seq, "ok": bool(e.payload.get("ok")), "on": e.payload.get("on"),
         "code": e.payload.get("code"), "message": str(e.payload.get("message", ""))}
        for e in view.events
        if e.type == "completion_check.evaluated" and e.run == run
        and e.instance == PORTFOLIO.supervisor
    ]  # fmt: skip
    done = next((x["seq"] for x in evaluations if x["ok"]), None)
    after = next((e.seq for e in view.events if e.type == "task.entered" and e.run == run
                  and e.instance == "table"), None)  # fmt: skip
    episode.save("portfolio", "goal", {"turns": turns_read, "evaluations": evaluations,
                                       "done_seq": done, "table_entered_seq": after})  # fmt: skip
