"""Pure rules the agent applies in code, never in model text.

1. :func:`next_action`: the recommendation rule table (one of three next actions).
2. :func:`family_of`: publications grouped into families by a shared priority number.
3. :func:`form_refusal`: what the form tool refuses (a field marked complete that counsel
   must confirm, a complete field with no value, a template field left out).

Nothing here gives a legal opinion: the rule table orders work for counsel; it does not
judge novelty, and ``not_found`` only ever means "no reference in this search shows it".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

NEXT_ACTIONS = ("prioritize_counsel_review", "collect_more_evidence", "deprioritize_for_now")
STATES = ("disclosed", "partial", "not_found", "not_checked")
FIELD_STATUSES = ("complete", "missing", "needs_counsel_confirmation")
BUILT = ("built_and_tested", "in_use_under_nda")


def element_states(comparison: Mapping[str, Any]) -> dict[str, list[str]]:
    """Each element's states across the families searched."""
    out: dict[str, list[str]] = {}
    for cell in comparison.get("cells", ()):
        out.setdefault(str(cell["element"]), []).append(str(cell["state"]))
    return out


def next_action(extraction: Mapping[str, Any], comparison: Mapping[str, Any]) -> tuple[str, str]:
    """The rule table: ``(next_action, rule_id)``. First matching row wins.

    R1 prioritise counsel review: an element no family in the search shows, the invention
       is built and tested (or running), no referenced evidence is missing, and it serves
       one of the top two business priorities.
    R2 collect more evidence: the invention is an idea only, or a referenced document is
       missing.
    R3 collect more evidence: the venture's own public disclosure describes it in full.
    R4 collect more evidence: it serves one of the top two business priorities.
    R5 deprioritise for now: everything else.
    """
    states = element_states(comparison)
    unseen = [e for e, s in states.items() if s and all(x == "not_found" for x in s)]
    status = str(extraction.get("implementation", {}).get("status", ""))
    missing_docs = bool(extraction.get("missing_documents"))
    rank = extraction.get("business_priority", {}).get("rank")
    top = isinstance(rank, int) and rank <= 2
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
    """Families: publications joined when they share a ``SYN-PRI`` priority number."""
    families: dict[str, list[str]] = {}
    for p in publications:
        numbers = [
            c["number"]
            for c in p.get("priority_claims", ())
            if str(c["number"]).startswith("SYN-PRI")
        ]
        head = numbers[0] if numbers else str(p["id"])
        families.setdefault(head, []).append(str(p["id"]))
    return {k: sorted(v) for k, v in sorted(families.items())}


def form_refusal(
    template: Mapping[str, Any], fields: Sequence[Mapping[str, Any]]
) -> tuple[str, str] | None:
    """Why the form tool refuses ``fields`` for ``template``, or None: ``(code, message)``."""
    declared = {f["field"]: f for f in template["fields"]}
    given = [str(f.get("field")) for f in fields]
    if sorted(given) != sorted(declared):
        missing = sorted(set(declared) - set(given))
        extra = sorted(set(given) - set(declared))
        return "fields_mismatch", f"missing {missing}, not in the template {extra}"
    for f in fields:
        name, status, value = str(f["field"]), f.get("status"), f.get("value")
        if status not in FIELD_STATUSES:
            return "status_invalid", f"{name}: status is one of {FIELD_STATUSES}"
        empty = value in (None, "", [], {})
        if status == "complete" and empty:
            return "complete_needs_value", f"{name}: a complete field holds a value"
        default = declared[name].get("default_status") or ""
        if status == "complete" and default.startswith("needs_counsel_confirmation"):
            return "counsel_decides", f"{name}: only counsel confirms this field"
        if status == "complete" and f.get("counsel_question"):
            return (
                "counsel_decides",
                f"{name}: a field with an open counsel question is not complete",
            )
    return None
