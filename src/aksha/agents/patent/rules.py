"""Pure rules the agent applies in code, never in model text.

1. :func:`next_action`: the recommendation rule table (one of three next actions).
2. :func:`family_of`: publications grouped into families by a shared priority number.
3. :func:`required_missing`: the required fields still missing, which keep a packet from
   becoming ready. What a fill of a form may hold (a counsel field never complete, a
   complete field with a value, every template field given) is the harness's form tool's
   rule (``runtime/forms``), checked in code for every agent that lists it.
4. :func:`research_misuse`: where a recommendation names a research output outside its
   business-importance part, which would use market research as novelty or prior-art
   evidence.
5. :func:`selection_options` and :func:`selection_from_answer`: the choice put to the
   person (the table's candidates with their recommendations) and the selection the
   runtime saves from the codes they chose. The person decides; the agent never does.

Nothing here gives a legal opinion: the rule table orders work for counsel; it does not
judge novelty, and ``not_found`` only ever means "no reference in this search shows it".
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

NEXT_ACTIONS = ("prioritize_counsel_review", "collect_more_evidence", "deprioritize_for_now")
STATES = ("disclosed", "partial", "not_found", "not_checked")
BUILT = ("built_and_tested", "in_use_under_nda")
IMPLEMENTATION = ("built_and_tested", "in_use_under_nda", "prototype", "idea_only", "unknown")
"""What ``extraction.implementation.status`` holds (declared in the extraction schema)."""
TOP_PRIORITIES = 2
"""How many top business priorities count as top when the request names no number."""
RESEARCH_KINDS = ("research.needs", "research.findings")
"""The research outputs a recommendation reads for business importance, by kind: what
customers need and what the research found. They are versioned artifacts of another agent,
read by ID and version; a run with no research output records business importance from
research as unknown."""
RESEARCH_NOTE = (
    "Research outputs support business importance only; they are not evidence of "
    "technical novelty or prior art."
)
"""What every recommendation states about the research it read (``business_importance.note``)."""
BUSINESS_PART = "business_importance"
"""The one part of a recommendation that may name a research output."""
_RESEARCH_REF = re.compile(r"\bresearch\.[a-z_]+(?:/[^@\s]+)?@v\d+")


def research_misuse(content: Mapping[str, Any]) -> list[str]:
    """Pointers outside ``business_importance`` that name a research output version.

    A cite of a research artifact, or any text naming one (``research.needs@v2``), outside
    the business-importance part would rest a novelty, prior-art or next-action claim on
    market research: the recommendation check refuses it.
    """
    out: list[str] = []

    def walk(value: Any, at: str) -> None:
        if isinstance(value, Mapping):
            for k, v in value.items():
                walk(v, f"{at}/{k}")
        elif isinstance(value, list | tuple):
            for i, v in enumerate(value):
                walk(v, f"{at}/{i}")
        elif isinstance(value, str) and _RESEARCH_REF.search(value):
            out.append(at)

    for k, v in content.items():
        if k != BUSINESS_PART:
            walk(v, f"/{k}")
    return out


def element_states(comparison: Mapping[str, Any]) -> dict[str, list[str]]:
    """Each element's states across the families searched."""
    out: dict[str, list[str]] = {}
    for cell in comparison.get("cells", ()):
        if isinstance(cell, Mapping) and cell.get("element") is not None:
            out.setdefault(str(cell["element"]), []).append(str(cell.get("state")))
    return out


def next_action(
    extraction: Mapping[str, Any], comparison: Mapping[str, Any], top_n: int = TOP_PRIORITIES
) -> tuple[str, str]:
    """The rule table: ``(next_action, rule_id)``. First matching row wins.

    R1 prioritise counsel review: an element no family in the search shows, the invention
       is built and tested (or running), no referenced evidence is missing, and it serves
       one of the top ``top_n`` business priorities (two unless the request names a number).
    R2 collect more evidence: the invention is an idea only, or a referenced document is
       missing.
    R3 collect more evidence: the venture's own public disclosure describes it in full.
    R4 collect more evidence: it serves one of the top ``top_n`` business priorities.
    R5 deprioritise for now: everything else.
    """
    states = element_states(comparison)
    unseen = [e for e, s in states.items() if s and all(x == "not_found" for x in s)]
    status = str(extraction.get("implementation", {}).get("status", ""))
    missing_docs = bool(extraction.get("missing_documents"))
    rank = extraction.get("business_priority", {}).get("rank")
    top = isinstance(rank, int) and not isinstance(rank, bool) and rank <= top_n
    full_public = any(
        d.get("public") == "yes" and d.get("describes_fully")
        for d in extraction.get("disclosures", ())
    )
    if unseen and status in BUILT and not missing_docs and top:
        return "prioritize_counsel_review", "R1"
    if status == "idea_only" or missing_docs:
        return "collect_more_evidence", "R2"
    if full_public:
        return "collect_more_evidence", "R3"
    if top:
        return "collect_more_evidence", "R4"
    return "deprioritize_for_now", "R5"


def family_of(publications: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Families: publications joined when they share a priority number (any office's).

    A family is named by its earliest priority: the earliest-dated claim that is a priority
    claim proper (not a ``relation`` such as "continuation of", which still joins the
    family). A publication that claims no priority is its own family, named by its number.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    for p in publications:
        numbers = [str(c["number"]) for c in p.get("priority_claims", ()) if c.get("number")]
        find(str(p["id"]))
        for n in numbers:
            a, b = find(str(p["id"])), find("prio:" + n)
            if a != b:
                parent[max(a, b)] = min(a, b)
    families: dict[str, list[str]] = {}
    for p in publications:
        root = find(str(p["id"]))
        names = sorted(
            (bool(c.get("relation")), str(c.get("date") or "9999"), str(c["number"]))
            for q in publications
            if find(str(q["id"])) == root
            for c in q.get("priority_claims", ())
            if c.get("number")
        )
        families.setdefault(names[0][2] if names else str(p["id"]), []).append(str(p["id"]))
    return {k: sorted(v) for k, v in sorted(families.items())}


def computed_recommendation(content: dict[str, Any]) -> dict[str, Any]:
    """A recommendation's ``business_importance.note``, written by code at every save.

    The sentence is fixed (``RESEARCH_NOTE``), so the model is never asked to copy it: a
    save that leaves it out, or words it differently, stores the rule's sentence.
    """
    part = content.get(BUSINESS_PART)
    if isinstance(part, dict):
        part["note"] = RESEARCH_NOTE
    return content


def required_missing(form: Mapping[str, Any]) -> list[str]:
    """The required fields of a filled form that are ``missing``: the packet is not ready."""
    return [
        str(f["field"])
        for f in form.get("fields", ())
        if f.get("required") and f.get("status") == "missing"
    ]


def _table_rows(view: Any) -> list[Mapping[str, Any]]:
    table = view.latest("table")
    rows = [] if table is None else table.content.get("rows", ())
    return [r for r in rows if isinstance(r, Mapping) and r.get("candidate")]


def _recommended(view: Any, code: str, row: Mapping[str, Any] | None) -> Mapping[str, Any]:
    """The code-checked recommendation of ``code`` (the table row only when there is none)."""
    rec = view.latest("recommendation", code)
    if rec is not None:
        found: Mapping[str, Any] = rec.content
        return found
    return row or {}


def selection_options(view: Any) -> tuple[tuple[str, str], ...]:
    """The choice the person makes: each table row's candidate, with its next action and why.

    The table gives only the codes (``checks.table_saved`` holds them to the candidates
    with a recommendation, no more and no fewer); each label is read from the candidate's
    code-checked recommendation, never from the text the model wrote in the table, so the
    model cannot leave a candidate out, add one, or word its recommendation for it.
    """
    out: list[tuple[str, str]] = []
    for row in _table_rows(view):
        code = str(row["candidate"])
        if code in {c for c, _ in out}:
            continue
        src = _recommended(view, code, row)
        label = f"{src.get('next_action', '')}: {src.get('why', '')}".strip(": ")
        out.append((code, label))
    return tuple(out)


def selection_from_answer(view: Any, chosen: tuple[str, ...]) -> dict[str, Any]:
    """The selection the runtime saves from the person's answer: the codes they chose.

    ``selected`` (written by the runtime too) is the list of chosen candidate codes, which
    ``prepare``, ``packet`` and ``send`` fan out over; ``recommendations`` keeps each
    chosen candidate's next action, from its recommendation, as it stood when the person
    chose.
    """
    table = view.latest("table")
    rows = {str(r["candidate"]): r for r in _table_rows(view)}
    out: dict[str, Any] = {
        "selected": list(chosen),
        "based_on": "" if table is None else str(table.ref),
        "recommendations": {
            c: _recommended(view, c, rows.get(c)).get("next_action") for c in chosen
        },
    }
    if not rows:
        out["note"] = "no invention candidate was found among the inputs; nothing was asked"
    elif not chosen:
        out["note"] = "the person chose none of the candidates"
    return out
