"""Pure completion checks. Each reads only the Task view it is given."""

from __future__ import annotations

from typing import Any

from aksha.runtime.api import CheckResult

from . import rules

K = "patent."


def _own(view: Any, kind: str) -> Any:
    return view.own(kind)


def portfolio_saved(view: Any) -> CheckResult:
    """The portfolio of candidates is saved."""
    return CheckResult(view.latest("portfolio") is not None, "save the portfolio")


def extraction_saved(view: Any) -> CheckResult:
    """This candidate's extraction is saved."""
    return CheckResult(_own(view, "extraction") is not None, "save the extraction")


def prior_art_saved(view: Any) -> CheckResult:
    """This candidate's prior-art record is saved."""
    return CheckResult(_own(view, "prior_art") is not None, "save the prior-art record")


def comparison_saved(view: Any) -> CheckResult:
    """This candidate's comparison matrix is saved, one cell per element and family."""
    v = _own(view, "comparison")
    if v is None:
        return CheckResult(False, "save the comparison matrix")
    cells = v.content.get("cells", [])
    bad = [c for c in cells if c.get("state") not in rules.STATES]
    return CheckResult(not bad, "every cell is disclosed, partial, not_found or not_checked")


def recommendation_follows_rules(view: Any) -> CheckResult:
    """The saved next action is the rule table's, from the current extraction and matrix."""
    rec, ext, cmp_ = (_own(view, k) for k in ("recommendation", "extraction", "comparison"))
    if rec is None:
        return CheckResult(False, "save the recommendation")
    if ext is None or cmp_ is None:
        return CheckResult(False, "the extraction and comparison must exist first")
    want, rule = rules.next_action(ext.content, cmp_.content)
    got = rec.content.get("next_action")
    return CheckResult(got == want, f"the rule table gives {want} ({rule}), not {got}")


def table_saved(view: Any) -> CheckResult:
    """The recommendation table covers every candidate that has a recommendation."""
    table = view.latest("table")
    if table is None:
        return CheckResult(False, "save the recommendation table")
    have = {r["candidate"] for r in table.content.get("rows", [])}
    want = {k for k in view.all_latest("recommendation") if k is not None}
    missing = sorted(want - have)
    return CheckResult(not missing, f"rows missing for {missing}", count=len(missing))


def selection_saved(view: Any) -> CheckResult:
    """The person's selection is saved."""
    return CheckResult(view.latest("selection") is not None, "save the person's selection")


def forms_filled(view: Any) -> CheckResult:
    """Both forms are filled for this candidate."""
    ok = _own(view, "disclosure") is not None and _own(view, "worksheet") is not None
    return CheckResult(ok, "fill the disclosure form and the worksheet")


def packet_saved(view: Any) -> CheckResult:
    """The packet opens with the counsel section and carries the scope note."""
    p = _own(view, "packet")
    if p is None:
        return CheckResult(False, "save the counsel-review packet")
    sections = p.content.get("sections", [])
    first = sections[0]["title"] if sections else ""
    ok = first == "Needs counsel confirmation" and bool(p.content.get("scope_note"))
    return CheckResult(ok, "open with 'Needs counsel confirmation' and carry the scope note")


def handoff_recorded(view: Any) -> CheckResult:
    """The send's receipt is recorded."""
    return CheckResult(_own(view, "handoff") is not None, "record the send receipt")
