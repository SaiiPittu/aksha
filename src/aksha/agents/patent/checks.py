"""Pure completion checks. Each reads only the Task view it is given.

A message states what was found, what was expected and the change that passes. The counts
a check could compare are computed by code: a form's ``counts`` by ``fill_form`` and again
at every save (the kind's ``compute``), the next action by the rule table, the review by
the portfolio extension; the model never counts.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

from aksha.extensions.portfolio import review, supervised
from aksha.runtime.api import CheckResult

from . import rules
from .portfolio import SPEC

K = "patent."
QUOTED = ("extraction", "prior_art", "comparison", "recommendation", "packet")
"""The kinds whose ``{file, quote}`` items must quote a passage a read tool returned."""


def _quotes(value: Any, path: str = "") -> Iterator[tuple[str, str, str]]:
    if isinstance(value, Mapping):
        f, q = value.get("file"), value.get("quote")
        if isinstance(f, str) and isinstance(q, str) and q:
            yield path, f, q
        for k, v in value.items():
            yield from _quotes(v, f"{path}/{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            yield from _quotes(v, f"{path}/{i}")


def ungrounded(view: Any, content: Mapping[str, Any]) -> list[str]:
    """The paths of quotes in ``content`` that no passage minted for their file holds.

    The read tools mint a span over every passage a view quotes, so a quote the model
    copied from what it read is inside one; a quote it made up, or edited, is in none.
    """
    texts: dict[str, list[str]] = {}
    for e in view.log.events:
        if e.type == "excerpts.minted":
            for s in e.payload.get("spans", ()):
                texts.setdefault(str(s.get("record_id")), []).append(str(s.get("text", "")))
    out = []
    for path, f, q in _quotes(content):
        if not any(q in t for t in texts.get(f, ())):
            out.append(path)
    return out


def _grounded(view: Any, kind: str) -> CheckResult | None:
    v = _own(view, kind)
    if v is None:
        return None
    bad = ungrounded(view, v.content)
    if bad:
        return CheckResult(False, f"quotes at {bad[:5]} are in no passage a tool returned for "
                           "their file: quote the record exactly", count=len(bad))  # fmt: skip
    return None


def plan_of(view: Any) -> Mapping[str, Any]:
    """The portfolio's plan (the request read into what to do)."""
    p = view.latest("portfolio")
    plan = p.content.get("plan") if p is not None else None
    return plan if isinstance(plan, Mapping) else {}


def _own(view: Any, kind: str) -> Any:
    return view.own(kind)


def portfolio_saved(view: Any) -> CheckResult:
    """The portfolio of candidates is saved."""
    return CheckResult(view.latest("portfolio") is not None, "save the portfolio")


def extraction_saved(view: Any) -> CheckResult:
    """This candidate's extraction is saved, every quote in it grounded in what was read."""
    if _own(view, "extraction") is None:
        return CheckResult(False, "save the extraction")
    return _grounded(view, "extraction") or CheckResult(True)


def prior_art_saved(view: Any) -> CheckResult:
    """This candidate's prior-art record is saved, its quotes grounded; skipped by the plan."""
    v = _own(view, "prior_art")
    if v is None:
        return CheckResult(False, "save the prior-art record")
    if v.content.get("skipped") and plan_of(view).get("prior_art", True):
        return CheckResult(False, "the plan asks for a prior-art search: search, do not skip")
    return _grounded(view, "prior_art") or CheckResult(True)


def comparison_saved(view: Any) -> CheckResult:
    """This candidate's comparison matrix is saved, one cell per element and family."""
    v = _own(view, "comparison")
    if v is None:
        return CheckResult(False, "save the comparison matrix")
    cells = v.content.get("cells", [])
    bad = [c for c in cells if not isinstance(c, Mapping) or c.get("state") not in rules.STATES
           or not c.get("element")]  # fmt: skip
    if bad:
        return CheckResult(False, "every cell names its element and is disclosed, partial, "
                           "not_found or not_checked")  # fmt: skip
    return _grounded(view, "comparison") or CheckResult(True)


def recommendation_follows_rules(view: Any) -> CheckResult:
    """The saved next action is the rule table's, from the current extraction and matrix."""
    rec, ext, cmp_ = (_own(view, k) for k in ("recommendation", "extraction", "comparison"))
    if rec is None:
        return CheckResult(False, "save the recommendation")
    if ext is None or cmp_ is None:
        return CheckResult(False, "the extraction and comparison must exist first")
    cutoff = int(plan_of(view).get("priority_cutoff", rules.TOP_PRIORITIES))
    want, rule = rules.next_action(ext.content, cmp_.content, top_n=cutoff)
    got = rec.content.get("next_action")
    if got != want:
        return CheckResult(
            False, f"the rule table gives next_action {want!r} ({rule}), but the saved "
            f"recommendation says {got!r}: save it again with next_action {want!r}",
        )  # fmt: skip
    return research_cited(view, rec.content)


def research_read(view: Any) -> list[str]:
    """The current version of each research output the recommendation reads, as refs."""
    found = (view.latest(kind) for kind in rules.RESEARCH_KINDS)
    return [str(v.ref) for v in found if v is not None]


def research_cited(view: Any, content: Mapping[str, Any]) -> CheckResult:
    """Business importance cites the research read by exact version, and only there.

    With research outputs present, ``business_importance.research`` cites each current
    version (``research.needs@v2``) and ``from_research`` is ``read``; with none, it cites
    nothing and ``from_research`` is ``unknown``. The note says plainly that research is no
    evidence of novelty, and a research output named anywhere else is refused
    (``rules.research_misuse``): market research never stands in for prior art.
    """
    misused = rules.research_misuse(content)
    if misused:
        return CheckResult(
            False, f"{', '.join(misused)} name a research output: research supports business "
            f"importance only and is not evidence of novelty or prior art; cite it only under "
            f"{rules.BUSINESS_PART}.research", count=len(misused),
        )  # fmt: skip
    part = content.get(rules.BUSINESS_PART)
    if not isinstance(part, Mapping):
        return CheckResult(False, f"save {rules.BUSINESS_PART}: the research read, cited")
    if part.get("note") != rules.RESEARCH_NOTE:
        return CheckResult(False, f"{rules.BUSINESS_PART}.note says: {rules.RESEARCH_NOTE}")
    want = research_read(view)
    items = part.get("research")
    cited = [
        str(i["cite"].get("artifact"))
        for i in (items if isinstance(items, list | tuple) else ())
        if isinstance(i, Mapping) and isinstance(i.get("cite"), Mapping)
    ]
    basis = "read" if want else "unknown"
    if sorted(cited) != sorted(want) or part.get("from_research") != basis:
        shown = ", ".join(want) or "none: the research agent has not run"
        return CheckResult(
            False, f"{rules.BUSINESS_PART}.research cites {sorted(cited)}, but the research "
            f"read is {shown}: cite each as {{\"cite\": {{\"artifact\": <ref>, \"path\": "
            f"\"\"}}}} and set from_research to {basis!r}",
        )  # fmt: skip
    return CheckResult(True)


def table_saved(view: Any) -> CheckResult:
    """The recommendation table covers every candidate that has a recommendation."""
    table = view.latest("table")
    if table is None:
        return CheckResult(False, "save the recommendation table")
    have = {r.get("candidate") for r in table.content.get("rows", ()) if isinstance(r, Mapping)}
    want = {k for k in view.all_latest("recommendation") if k is not None}
    missing = sorted(want - have)
    if missing:
        return CheckResult(
            False, f"the table has no row for candidates {missing}: add one row per candidate "
            f"with a recommendation ({sorted(want)})", count=len(missing),
        )  # fmt: skip
    if not supervised(view, SPEC):
        return CheckResult(True)
    expected = review(view, SPEC)
    got = table.content.get("review")
    message = (
        "the table's review must list the decisions, the drop list and the audit as "
        f"recorded: {json.dumps(expected)}"
    )
    return CheckResult(_plain(got) == expected, message)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _forms(view: Any) -> dict[str, Any]:
    """This candidate's filled forms, by template."""
    prefix = f"{view.key}~"
    return {
        str(k)[len(prefix) :]: v
        for k, v in view.all_latest("form").items()
        if k is not None and str(k).startswith(prefix)
    }


def forms_filled(view: Any) -> CheckResult:
    """Every form the plan names is filled for this candidate (at least one form)."""
    filled = _forms(view)
    want = [str(t) for t in plan_of(view).get("forms", ())]
    missing = [t for t in want if t not in filled]
    if missing:
        return CheckResult(False, f"fill the forms {missing}", keys=tuple(missing))
    return CheckResult(bool(filled), "fill each blank form found (list_templates)")


def packet_saved(view: Any) -> CheckResult:
    """The packet opens with the counsel section, carries the scope note, and is ready.

    Ready means no required field of either form is ``missing`` (CNS-01): a missing
    required field keeps the packet from ready, so nothing downstream sends it.
    """
    p = _own(view, "packet")
    if p is None:
        return CheckResult(False, "save the counsel-review packet")
    sections = p.content.get("sections", [])
    first = sections[0].get("title") if sections and isinstance(sections[0], Mapping) else ""
    ok = first == "Needs counsel confirmation" and bool(p.content.get("scope_note"))
    if not ok:
        return CheckResult(False, "open with 'Needs counsel confirmation' and carry the scope note")
    forms = list(_forms(view).values())
    if not forms:
        return CheckResult(False, "the forms must be filled first")
    bad = _grounded(view, "packet")
    if bad is not None:
        return bad
    missing = [n for f in forms for n in rules.required_missing(f.content)]
    return CheckResult(
        not missing,
        f"a packet with a missing required field is not ready: {missing}",
        count=len(missing),
    )


def handoff_recorded(view: Any) -> CheckResult:
    """The send's receipt is recorded; a plan that sends nothing records the packet held."""
    h = _own(view, "handoff")
    if h is None:
        return CheckResult(False, "record the send receipt")
    if h.content.get("status") == "held" and plan_of(view).get("send", True):
        return CheckResult(False, "the plan asks for the packet to be sent")
    return CheckResult(True)
