"""Pure completion checks for the research agent. Each reads only the ``TaskView`` it is given.

A check's message states what it found, what it expected and the exact change that
passes, so a model can fix its work in one turn. The counts a check compares (companies
resolved, pages found, review rows, each theme's "N of M") are computed at save
(:mod:`.computed`), never asked of the model; a check still holds them, against a
correction or a malformed save, and says the computed value.

Grounding (every cite verified, unknowns only where declared) is the runtime's, at save;
these checks hold the agent's own rules on top. What to compare is the scope's,
taken from the request: the scope lists its ``dimensions`` (the matrix columns besides
price) and its ``themes`` (what to read the reviews for), and every later check holds the
work to that list, so no column or theme is fixed in code.

The competitors carry the scope's dimensions on, so each company's evidence reads only the
competitors: a scope change that leaves the dimensions as they were rebuilds the
competitors equal and stops there (early cutoff, ADR-P05).

The rules: one record per company with its merge evidence; a price conflict kept with
both values; "N of M reviews in this sample"; every company in the matrix with every
dimension cited or an explicit unknown; every proposed threshold marked ``proposal``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from aksha.runtime.api import CheckResult

from . import analysis, computed

PHRASE = re.compile(r"^(\d+) of (\d+) reviews in this sample$")
OUTPUTS = ("matrix", "needs", "findings", "tests")


def _content(view: Any, kind: str, key: str | None = None) -> Mapping[str, Any] | None:
    found = view.latest(kind, key)
    return None if found is None else found.content


def dimension_ids(scope: Mapping[str, Any] | None) -> list[str]:
    """The comparison dimensions of a scope (or of the competitors, which carry them on)."""
    if not scope:
        return []
    return [str(d.get("id")) for d in scope.get("dimensions", ()) if isinstance(d, Mapping)]


PLAN_KINDS = ("comparison", "synthesis")
"""A market comparison (sellers, prices, a matrix) or a synthesis of what the inputs say."""


def plan_kind(content: Mapping[str, Any] | None) -> str:
    """The plan a scope (or the competitors, which carry it on) names; ``comparison`` if none."""
    plan = (content or {}).get("plan")
    return str(plan.get("kind")) if isinstance(plan, Mapping) else "comparison"


def scope_done(view: Any) -> CheckResult:
    """The scope names its plan, a boundary and what to compare or read for, with sources.

    A comparison also names its segment and geography; a synthesis needs neither.
    """
    scope = _content(view, "scope")
    if scope is None:
        return CheckResult(False, "save the scope")
    plan = scope.get("plan")
    if not isinstance(plan, Mapping) or plan.get("kind") not in PLAN_KINDS:
        got = plan.get("kind") if isinstance(plan, Mapping) else plan
        return CheckResult(
            False, f"the scope's plan kind is {got!r}: set plan.kind to one of {list(PLAN_KINDS)}"
        )
    comparison = plan.get("kind") == "comparison"
    needed = ("segment", "geography") if comparison else ()
    given = {f: v if isinstance(v := scope.get(f), Mapping) else {} for f in needed}
    missing = [f for f in needed if not given[f].get("value")]
    if missing:
        return CheckResult(
            False, f"scope has no value for {missing}: give each a value and its basis",
            keys=tuple(missing),
        )  # fmt: skip
    unsourced = [f for f in needed if not given[f].get("basis")]
    if unsourced:
        return CheckResult(
            False, f"{unsourced} have no basis: say where each came from (request, answer, "
            "an input)", keys=tuple(unsourced),
        )  # fmt: skip
    boundary = scope.get("boundary")
    if not isinstance(boundary, Mapping) or not boundary.get("in") or "out" not in boundary:
        return CheckResult(False, "the scope needs a boundary: what is in and what is out")
    dims = scope.get("dimensions")
    if not isinstance(dims, list | tuple):
        return CheckResult(False, "list the dimensions to compare (an empty list compares price)")
    bad = [d for d in dims if not isinstance(d, Mapping) or not d.get("id")]
    ids = dimension_ids(scope)
    if bad or len(ids) != len(set(ids)):
        twice = sorted({i for i in ids if ids.count(i) > 1})
        return CheckResult(
            False, f"{len(bad)} dimensions have no id and ids {twice} repeat: give each "
            "dimension its own id",
        )  # fmt: skip
    themes = scope.get("themes")
    if not isinstance(themes, list | tuple) or any(
        not isinstance(t, Mapping) or not t.get("id") for t in themes
    ):
        return CheckResult(
            False, "list the themes to read the customer voices for, each with an id"
        )
    if not comparison and not themes:
        return CheckResult(False, "a synthesis names the themes it reads the inputs for")
    return CheckResult(True)


def _relations(view: Any, kind: str) -> list[Mapping[str, Any]]:
    return [
        e.payload
        for e in view.log.events
        if e.type == "relation.saved" and str(e.payload.get("kind", "")).endswith(f".{kind}")
    ]


def _joined(pages: list[str], links: list[Mapping[str, Any]]) -> bool:
    """Whether ``links`` (relations with ``src`` and ``dst`` records) connect all ``pages``."""
    group = {pages[0]}
    grew = True
    while grew:
        grew = False
        for link in links:
            ends = {str(link.get("src", {}).get("id")), str(link.get("dst", {}).get("id"))}
            if len(ends & group) == 1 and ends <= set(pages):
                group |= ends
                grew = True
    return group == set(pages)


def _objects(listed: Any) -> list[Mapping[str, Any]]:
    """The objects in a saved list (a malformed list, or item, is left out)."""
    return [i for i in listed if isinstance(i, Mapping)] if isinstance(listed, list | tuple) else []


def _companies(found: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The competitors' companies that are objects."""
    return _objects(found.get("companies"))


def _pages(company: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """A company's pages that are objects."""
    return _objects(company.get("pages"))


def _key(company: Mapping[str, Any]) -> str:
    """A company's key (computed at save when the model gave none)."""
    return str(company.get("key"))


def _unkeyed(companies: list[Mapping[str, Any]]) -> CheckResult | None:
    """A failure naming the companies with no key, or None (the key is computed at save)."""
    bad = [i for i, c in enumerate(companies) if not computed.valid_key(c.get("key"))]
    if not bad:
        return None
    return CheckResult(
        False, f"companies at {bad} have no key and no canonical name or page to compute "
        "one from: give each a canonical name (the key is computed from it at save)",
    )  # fmt: skip


EXCLUSION_REASONS = (
    "copy",
    "older_version",
    "advert",
    "other_subject",
    "not_a_seller",
    "out_of_scope",
)
"""Why a record read in discover may be left out of the competitors: the instructions' rule
on search results and the scope's boundary, and only those. A copy of a page on another
site, an older version of a page, an advert or deal site, a page that only shares a word, a
record that sells nothing, or a page that sells it but not in the scope's segment or
geography (or is the venture's own offer)."""

DEFINITIONS = "definitions/"
"""The prefix of a definition record's ID: the runtime's, never a page the agent judged."""

WEB_SEARCH = ".web_search?"
"""What a recorded web search's query ID holds after the agent's name."""


def _absence(found: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The scope of the scoped absence claim ``found`` holds at ``absence``, or None.

    The scope is verified at save by the runtime, which refuses a scope never read here.
    """
    node = found.get("absence")
    if not isinstance(node, Mapping) or set(node) != {"absence"}:
        return None
    inner = node["absence"]
    if not isinstance(inner, Mapping):
        return None
    statement, scope = inner.get("statement"), inner.get("scope")
    if not isinstance(statement, str) or not statement.strip():
        return None
    return scope if isinstance(scope, Mapping) and scope else None


def _scoped_queries(scope: Mapping[str, Any]) -> set[str]:
    """The query IDs an absence's scope names (``query``: one ID or a list of them)."""
    query = scope.get("query")
    listed = [query] if isinstance(query, str) else query if isinstance(query, list | tuple) else []
    # A query ID may hold "@" in its arguments: only a recorded read's hash is cut off.
    return {
        str(q)[len("query:") :].rpartition("@")[0] if str(q).startswith("query:") else str(q)
        for q in listed
    }


def instance_reads(view: Any) -> list[Mapping[str, Any]]:
    """The lineage reads this instance recorded, in order, since its last rebuild.

    Only the reads since the instance's last rebuild count, as for lineage: a record read
    for an earlier version of the work is not one this version has to account for.
    """
    ref = getattr(view, "instance", None)
    run, instance = getattr(ref, "run", None), getattr(ref, "instance", None)
    mine = [
        e
        for e in view.log.events
        if getattr(e, "run", run) == run and getattr(e, "instance", instance) == instance
    ]
    since = max(
        (i for i, e in enumerate(mine)
         if e.type == "task.entered" and e.payload.get("cause") == "rebuild"),
        default=-1,
    )  # fmt: skip
    out: list[Mapping[str, Any]] = []
    for e in mine[since + 1 :]:
        read = e.payload.get("input") if e.type == "read.recorded" else None
        if isinstance(read, Mapping):
            out.append(read)
    return out


def records_read(view: Any) -> list[str]:
    """Every record this instance read since its last rebuild: web pages and inputs alike.

    A definition the runtime reads for its own tools is not one the agent judged, so it
    is left out. A part read (``<id>#...``) counts as its record.
    """
    out: dict[str, None] = {}
    for read in instance_reads(view):
        rid = str(read.get("id", "")).partition("#")[0]
        if read.get("kind") == "record" and rid and not rid.startswith(DEFINITIONS):
            out.setdefault(rid, None)
    return list(out)


def web_searches(view: Any) -> list[str]:
    """The query IDs of every web search this instance ran since its last rebuild."""
    out: dict[str, None] = {}
    for read in instance_reads(view):
        qid = str(read.get("id", ""))
        if read.get("kind") == "query" and WEB_SEARCH in qid:
            out.setdefault(qid, None)
    return list(out)


def _span_records(view: Any) -> dict[str, str]:
    """Every minted span's record ID by span ID (``excerpts.minted``), parts as their record."""
    out: dict[str, str] = {}
    for e in view.log.events:
        if e.type != "excerpts.minted":
            continue
        for span in e.payload.get("spans", ()):
            if isinstance(span, Mapping) and span.get("span_id"):
                out[str(span["span_id"])] = str(span.get("record_id", "")).partition("#")[0]
    return out


def _excluded_record(entry: Mapping[str, Any]) -> str:
    """The record an exclusion names (``record:`` read text or a bare ID), as a bare ID."""
    rec = str(entry.get("record") or "")
    rec = rec[len("record:") :] if rec.startswith("record:") else rec
    rec = rec.rpartition("@")[0] if "@" in rec else rec
    return rec.partition("#")[0]


def _words(text: object) -> str:
    """``text`` with whitespace runs collapsed and case folded, to compare two quotes."""
    return " ".join(str(text or "").split()).casefold()


def _empty_comparison(view: Any, found: Mapping[str, Any]) -> CheckResult | None:
    """A failure for a comparison that lists no company without accounting for it, or None.

    An empty list is a claim that nobody sells what the request is about. It stands only as
    a scoped absence claim whose scope names every web search the instance ran, and only
    when every record the instance read (web pages and inputs alike) is left out for a reason
    the instructions allow, each cited by a span of that record itself, in words that page
    alone shows: words cited to leave out two pages are a label every page carries, never a
    reason about one of them.

    The scope keys the claim: an input search or listing whose results changed reopens it,
    and so does a scoped page that moved. The runtime never runs a web search again (it has
    no web when a source changes), so an empty web comparison reopens when a page it scoped
    or cited changes, not when a new page appears on the web.
    """
    searches = web_searches(view)
    shape = (
        '{"absence": {"statement": "<what was not found>", "scope": {"query": [<the '
        '"recorded" query of every web_search you ran, and of each list_inputs or '
        'search_inputs result you rest on>], "records": ["record:<id>@<version>", ...]}}}'
    )
    scope = _absence(found)
    if scope is None:
        return CheckResult(
            False, "the competitors list no company, but the scope plans a comparison: list "
            "each company whose own page sells what the request is about, from the pages and "
            "inputs you read. Only if none does, save absence as a scoped claim, " + shape
            + ", and list every page and input you read under excluded",
        )  # fmt: skip
    unscoped = [q for q in searches if q not in _scoped_queries(scope)]
    if unscoped:
        return CheckResult(
            False, f"the absence's scope leaves out the web searches {unscoped}: an empty "
            "market rests on every search that found nothing to list, so name each one's "
            f'recorded query in absence.scope.query (a list), as in {shape}',
        )  # fmt: skip
    entries = _objects(found.get("excluded"))
    bad = [
        _excluded_record(e) or str(i)
        for i, e in enumerate(entries)
        if e.get("reason") not in EXCLUSION_REASONS or not isinstance(e.get("cite"), Mapping)
    ]
    if bad:
        return CheckResult(
            False, f"excluded records {bad} give no allowed reason or no cite: a record is "
            f"left out only as one of {list(EXCLUSION_REASONS)} (a copy on another site, an "
            "older version, an advert or deal site, a page about something else, a record "
            "that sells nothing, a page that sells it outside the scope's segment or "
            "geography), with the cite from that record that shows it. A page that sells "
            "what the request is about in the scope is a competitor: list its company",
        )  # fmt: skip
    spans = _span_records(view)
    elsewhere = [
        _excluded_record(e)
        for e in entries
        if spans.get(str(e["cite"].get("span", ""))) != _excluded_record(e)
    ]
    if elsewhere:
        return CheckResult(
            False, f"the cites that leave out {elsewhere} are not spans of those records: "
            "cite the passage of the excluded record itself that shows why it is left out",
        )  # fmt: skip
    by_quote: dict[str, list[str]] = {}
    for e in entries:
        quote = _words(e["cite"].get("quote"))
        if quote:
            by_quote.setdefault(quote, [])
            if _excluded_record(e) not in by_quote[quote]:
                by_quote[quote].append(_excluded_record(e))
    shared = {q: rs for q, rs in by_quote.items() if len(rs) > 1}
    if shared:
        quote, records = next(iter(shared.items()))
        return CheckResult(
            False, f"the same words {quote!r} leave out the records {records}: words every "
            "page carries are a label, not a reason about one page. Exclude a page only on "
            "what that page itself shows, or list its company",
        )  # fmt: skip
    accounted = {_excluded_record(e) for e in entries}
    unaccounted = [r for r in records_read(view) if r not in accounted]
    if unaccounted:
        return CheckResult(
            False, f"the competitors list no company, but the records {unaccounted} were read "
            "and are neither a company's page nor excluded: list each page's company, or "
            f"exclude each record with its record ID, a reason from {list(EXCLUSION_REASONS)} "
            "and a cite of that record",
        )  # fmt: skip
    return None


def discover_done(view: Any) -> CheckResult:
    """Each page belongs to one company; a merged company's pages are joined by ``same_entity_as``.

    The relations must join exactly that company's pages: a relation between other pages,
    or one fewer than the merge needs, leaves the check failing. A comparison that lists no
    company passes only as a scoped absence claim over every web search it ran, with every
    record it read excluded for an allowed reason, each cited by that record's own words
    (:func:`_empty_comparison`); a synthesis lists none and needs neither.
    """
    found = _content(view, "competitors")
    if found is None:
        return CheckResult(False, "save the competitors")
    companies = _companies(found)
    unkeyed = _unkeyed(companies)
    if unkeyed is not None:
        return unkeyed
    if not companies and plan_kind(_content(view, "scope") or found) == "comparison":
        empty = _empty_comparison(view, found)
        if empty is not None:
            return empty
    norecord = [_key(c) for c in companies if any(not p.get("record") for p in _pages(c))]
    if norecord:
        return CheckResult(
            False, f"companies {norecord} list a page with no record: give each page the "
            "record it was read from", keys=tuple(norecord),
        )  # fmt: skip
    # A page is a record, or one section of a record that holds several sellers' sections.
    pages = [(str(p["record"]), p.get("section")) for c in companies for p in _pages(c)]
    if len(pages) != len(set(pages)):
        owners: dict[tuple[str, Any], list[str]] = {}
        for c in companies:
            for p in _pages(c):
                owners.setdefault((str(p["record"]), p.get("section")), []).append(_key(c))
        shared = {_page_name(p): ks for p, ks in owners.items() if len(ks) > 1}
        return CheckResult(
            False, f"pages listed under more than one company: {shared}; a page belongs to "
            "one company: keep each under one and merge the companies if they are one",
        )  # fmt: skip
    counted = (found.get("resolved"), found.get("pages_found"))
    if counted != (len(companies), len(pages)):
        return CheckResult(
            False, f"resolved is {counted[0]!r} and pages_found {counted[1]!r}, but the "
            f"competitors list {len(companies)} companies on {len(pages)} pages: set resolved "
            f"to {len(companies)} and pages_found to {len(pages)}",
        )  # fmt: skip
    merged = [c for c in companies if len(_pages(c)) > 1]
    linked = _relations(view, "same_entity_as")
    unlinked = {
        _key(c): [str(p["record"]) for p in _pages(c)]
        for c in merged
        if not _joined([str(p["record"]) for p in _pages(c)], linked)
    }
    if unlinked:
        return CheckResult(
            False, f"companies {list(unlinked)} list several pages that no same_entity_as "
            f"relation joins: save the competitors again with same_entity_as relations "
            f"joining each one's pages {unlinked}, each with the excerpt that says they are "
            "one company", keys=tuple(unlinked),
        )  # fmt: skip
    unproven = [_key(c) for c in merged if not c.get("merge_evidence")]
    if unproven:
        return CheckResult(
            False, f"companies {unproven} merge several pages but their merge_evidence is "
            "empty: cite in each the excerpt that says the pages are one company",
            keys=tuple(unproven),
        )  # fmt: skip
    return CheckResult(True)


def _page_name(page: tuple[str, Any]) -> str:
    return page[0] if page[1] is None else f"{page[0]} section {page[1]}"


def _missing_cells(entry: Mapping[str, Any], dims: list[str]) -> list[str]:
    cells = entry.get("cells", {})
    out = [] if entry.get("prices") else ["prices"]
    return out + [d for d in dims if not isinstance(cells, Mapping) or not cells.get(d)]


def profile_done(view: Any) -> CheckResult:
    """The company's profile has prices and every scope dimension; a conflict is kept open."""
    profile = _content(view, "profile", view.key)
    if profile is None:
        return CheckResult(False, f"save the profile of {view.key}")
    empty = _missing_cells(profile, dimension_ids(_content(view, "competitors")))
    if empty:
        return CheckResult(
            False, f"the profile of {view.key} has no entry in {empty}: add to each a cited "
            "entry, or an explicit unknown with its question and reason",
        )  # fmt: skip
    points = [
        {"record": e.get("record"), "per": e.get("per"), "currency": e.get("currency"),
         "value": e["value"]}
        for e in profile.get("prices", ())
        if isinstance(e, Mapping) and "value" in e
    ]  # fmt: skip
    recorded = {
        str(c.get("per"))
        for c in profile.get("conflicts", ())
        if isinstance(c, Mapping) and c.get("field") == "price"
    }
    open_ = [b for b in analysis.price_conflict(points) if b not in recorded]
    if open_:
        entries = [{"field": "price", "per": b} for b in open_]
        return CheckResult(
            False, f"prices per {open_} differ across pages and conflicts records none of "
            f"them: add {entries} to conflicts, keeping every price as listed",
        )  # fmt: skip
    return CheckResult(True)


def needs_done(view: Any) -> CheckResult:
    """Every scope theme is counted as "N of M reviews in this sample", M the unique rows."""
    found = _content(view, "needs")
    if found is None:
        return CheckResult(False, "save the needs")
    sample = found.get("sample")
    sample = sample if isinstance(sample, Mapping) else {}
    unique, rows = sample.get("unique"), sample.get("rows")
    duplicates = sample.get("duplicates", ())
    copies = len(duplicates) if isinstance(duplicates, list | tuple) else 0
    needs = _objects(found.get("needs"))
    if not isinstance(unique, int) or isinstance(unique, bool) or unique < 0:
        return CheckResult(
            False, f"sample.unique is {unique!r}: list in sample.reviews the ID of every "
            "review row read, cross-posted copies included, and each copy in "
            "sample.duplicates (rows, unique and each need's phrase are computed from them "
            "at save)",
        )  # fmt: skip
    if rows != unique + copies:
        return CheckResult(
            False, f"sample.rows is {rows!r}, but {unique} unique reviews and {copies} "
            f"duplicates make {unique + copies}: set rows to {unique + copies}",
        )  # fmt: skip
    distinct = {
        str(e["review"])
        for n in needs
        for e in computed.cited_reviews(n) or []
        if isinstance(e, Mapping) and e.get("review") is not None
    }
    if len(distinct) > unique:
        return CheckResult(
            False, f"sample.unique is {unique}, but the needs cite {len(distinct)} distinct "
            f"reviews: set unique to the number of distinct reviews read, at least "
            f"{len(distinct)}",
        )  # fmt: skip
    scope = _content(view, "scope") or {}
    want = {str(t.get("id")) for t in scope.get("themes", ()) if isinstance(t, Mapping)}
    if (
        want
        and unique == 0
        and (sample.get("status") != "no_feedback_found" or not sample.get("checked"))
    ):
        return CheckResult(False, "no customer voice was read: read them, or say "
                           "status no_feedback_found with the listings checked")  # fmt: skip
    resolve = computed.theme_resolver(scope)
    unknown = [
        str(computed.need_theme(n)) for n in needs if resolve(computed.need_theme(n)) is None
    ]
    have = {t for n in needs if (t := resolve(computed.need_theme(n))) is not None}
    if want and unknown and want - have:
        return CheckResult(
            False, f"needs name themes {unknown} that the scope does not have: set each "
            f"need's theme to one of the scope's theme IDs {sorted(want)}, one need per "
            "theme, keeping the needs already saved",
            keys=tuple(sorted(want - have)),
        )  # fmt: skip
    if want - have:
        missing = sorted(want - have)
        return CheckResult(
            False, f"needs saved for themes {sorted(have & want)}; missing a need for themes "
            f"{missing}: save the needs again with one need per theme of the scope "
            f"({sorted(want)}), keeping the needs already saved",
            keys=tuple(missing),
        )  # fmt: skip
    bad = {}
    for need in needs:
        got = str(need.get("phrase", ""))
        m = PHRASE.match(got)
        n = len(computed.cited_reviews(need) or [])
        if m is None or int(m.group(1)) != n or int(m.group(2)) != unique:
            bad[str(resolve(computed.need_theme(need)) or computed.need_theme(need))] = (
                got, computed.phrase(n, unique)
            )  # fmt: skip
    if bad:
        said = "; ".join(f"{t}: {g!r}, expected {w!r}" for t, (g, w) in bad.items())
        return CheckResult(
            False, f"needs {sorted(bad)} do not say 'N of M reviews in this sample' with N "
            f"the reviews each cites and M the {unique} unique reviews ({said}): set each "
            "phrase to the expected one",
            keys=tuple(bad),
        )  # fmt: skip
    return CheckResult(True)


def _names(company: Mapping[str, Any]) -> set[str]:
    """A company's names, case folded: its canonical name and its aliases."""
    found = [company.get("canonical"), company.get("name"), *(company.get("aliases") or ())]
    names = (n.get("value") if isinstance(n, Mapping) else n for n in found)
    return {" ".join(n.split()).casefold() for n in names if isinstance(n, str) and n.strip()}


def _row_key(row: Mapping[str, Any], companies: list[Mapping[str, Any]]) -> str:
    """The company a matrix row is for: its key when a company has it, else by the name."""
    key = str(row.get("key"))
    if any(_key(c) == key for c in companies):
        return key
    named = row.get("company")
    name = named.get("value") if isinstance(named, Mapping) else named
    if isinstance(name, str):
        folded = " ".join(name.split()).casefold()
        match = [c for c in companies if folded in _names(c)]
        if len(match) == 1:
            return _key(match[0])
    return key


def report_done(view: Any) -> CheckResult:
    """The matrix has a row per company with every dimension; findings and tests are saved."""
    matrix = _content(view, "matrix")
    companies = _content(view, "competitors") or {}
    if matrix is None:
        return CheckResult(False, "save the matrix")
    listed = _companies(companies)
    unkeyed = _unkeyed(listed)
    if unkeyed is not None:
        return unkeyed
    rows = _objects(matrix.get("rows"))
    want = sorted(_key(c) for c in listed)
    have = sorted(_row_key(r, listed) for r in rows)
    if want != have:
        add, drop = sorted(set(want) - set(have)), sorted(set(have) - set(want))
        twice = sorted({k for k in have if have.count(k) > 1})
        return CheckResult(
            False, f"the matrix has rows {have}, but the companies are {want}: add rows for "
            f"{add}, remove rows for {drop} and keep one row per company (repeated: {twice})",
        )  # fmt: skip
    dims = dimension_ids(_content(view, "scope"))
    empty = [f"{_row_key(r, listed)}/{c}" for r in rows for c in _missing_cells(r, dims)]
    if empty:
        return CheckResult(
            False, f"matrix cells {empty} are empty: add to each a cited entry, or an "
            "explicit unknown with its question and reason",
        )  # fmt: skip
    findings = _content(view, "findings")
    if findings is None:
        return CheckResult(False, "save the findings")
    tests = _content(view, "tests")
    if plan_kind(_content(view, "scope")) == "synthesis" and tests is None:
        return CheckResult(True)
    if tests is None or "tests" not in tests:
        return CheckResult(False, "save what to test next")
    unmarked = [
        t.get("id") if isinstance(t, Mapping) else t
        for t in tests["tests"]
        if not isinstance(t, Mapping)
        or not isinstance(t.get("threshold", {}), Mapping)
        or t.get("threshold", {}).get("status") != "proposal"
    ]
    if unmarked:
        return CheckResult(
            False, f"tests {unmarked} have a threshold whose status is not 'proposal': set "
            "threshold.status to 'proposal' in each",
        )  # fmt: skip
    return CheckResult(True)


def outputs_exist(view: Any) -> CheckResult:
    """The run's outputs exist at current versions: a synthesis needs no tests."""
    want = OUTPUTS if plan_kind(_content(view, "scope")) == "comparison" else OUTPUTS[:3]
    missing = [k for k in want if view.latest(k) is None]
    return CheckResult(
        not missing, f"outputs {missing} are not saved: save each ({list(want)} make the run)",
        keys=tuple(missing),
    )  # fmt: skip
