"""Scoped claims (ADR-P13 amendment M1): an absence or a coverage claim names its scope.

A cite grounds a claim that something *is* stated: the words that state it. A claim that
something is *not* there ("no seller offers X", "no earlier publication discloses Y") or that a
count covers a whole ("4 of the 12 reviews mention setup") rests on no words at all: it
rests on what was looked at. Such a claim is written as one of two nodes, each an object
whose one key is its form:

1. ``{"absence": {"statement": "...", "scope": SCOPE}}``;
2. ``{"coverage": {"statement": "...", "count": 4, "of": 12, "scope": SCOPE}}`` (``of``
   optional).

``SCOPE`` holds ``query``, a recorded search or listing the instance ran (its query ID,
``<agent>.search?{...}``; the read's version is the hit set's hash), and/or
``records``, the records read in full, as read text (``record:pages/P-0001@3``), the same
form an explicit unknown's ``checked`` takes. The save path refuses a claim node with no
scope (``scope_missing``), a malformed one (``scope_invalid``) and a scope that names a
search or record the instance never read, or a record that has moved since
(``scope_not_read``). A refused save names the pointer and says what to add.

The scope is what invalidation keys the claim on (ADR-P05 amendment M2): a re-executed
search whose hit set changed, or a scoped record that moved, reopens the version even
when it cites nothing that changed. Pure; nothing here reads a store.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

ClaimForm = Literal["absence", "coverage"]
"""The two scoped claim forms."""

CLAIM_FORMS: frozenset[str] = frozenset(get_args(ClaimForm))

ScopeCode = Literal["scope_missing", "scope_invalid", "scope_not_read"]
"""Why a scoped claim was refused at save."""

SCOPE_HELP = (
    "an absence or coverage claim names the scope it rests on: "
    '{"scope": {"query": "<the recorded search or listing>"}} or '
    '{"scope": {"records": ["record:<id>@<version>", ...]}}, so a new matching source '
    "or a changed record can reopen it"
)
"""What a refusal tells the model to add."""


@dataclass(frozen=True)
class Scope:
    """What a scoped claim rests on: recorded queries by ID, and records read in full.

    ``records`` holds read text (``record:<id>@<version>``) as the claim gave it.
    """

    queries: tuple[str, ...] = ()
    records: tuple[str, ...] = ()

    def record_ids(self) -> frozenset[str]:
        """The scoped records' IDs, whole (a part's fragment and the version dropped)."""
        return frozenset(record_id(r) for r in self.records)


@dataclass(frozen=True)
class ScopedClaim:
    """One absence or coverage claim found in content, at ``pointer``."""

    form: ClaimForm
    pointer: str
    statement: str
    scope: Scope
    count: int | None = None
    of: int | None = None


@dataclass(frozen=True)
class ClaimProblem:
    """A claim node the save path refuses: the code, the pointer and why."""

    code: ScopeCode
    pointer: str
    message: str


def record_id(read_text: str) -> str:
    """``record:input/a.csv#rows/R1@v3`` to ``input/a.csv``: the whole record's ID."""
    body = read_text.split(":", 1)[1] if read_text.startswith("record:") else read_text
    body = body.rpartition("@")[0] or body
    return body.partition("#")[0]


def is_claim_node(value: Any) -> bool:
    """Whether ``value`` is shaped like a scoped claim: an object whose one key is a form."""
    return isinstance(value, Mapping) and len(value) == 1 and next(iter(value)) in CLAIM_FORMS


def _pointer(parts: Sequence[str | int]) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def claim_nodes(value: Any, parts: tuple[str | int, ...] = ()) -> Iterator[tuple[str, Any]]:
    """Every scoped claim node in ``value``, as ``(pointer, node)``; not inside one."""
    if is_claim_node(value):
        yield _pointer(parts), value
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from claim_nodes(child, (*parts, key))
    elif isinstance(value, list | tuple):
        for i, child in enumerate(value):
            yield from claim_nodes(child, (*parts, i))


def parse_claim(node: Mapping[str, Any], pointer: str = "") -> ScopedClaim | ClaimProblem:
    """The claim ``node`` holds, or why it is not a well-formed scoped claim."""
    form = next(iter(node))
    inner = node[form]
    if not isinstance(inner, Mapping):
        return ClaimProblem("scope_missing", pointer, f"{form} states no scope: {SCOPE_HELP}")
    statement = inner.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        return ClaimProblem("scope_invalid", pointer, f"{form} states its claim as a statement")
    scope = inner.get("scope")
    if scope is None:
        return ClaimProblem("scope_missing", pointer, f"{statement!r} has no scope: {SCOPE_HELP}")
    parsed = _scope(scope)
    if parsed is None:
        return ClaimProblem("scope_invalid", pointer, f"{statement!r}: {SCOPE_HELP}")
    count, of = inner.get("count"), inner.get("of")
    if form == "coverage" and not _count(count):
        return ClaimProblem("scope_invalid", pointer, "a coverage claim states its count")
    if of is not None and not _count(of):
        return ClaimProblem("scope_invalid", pointer, "of is a count")
    extra = set(inner) - {"statement", "scope", "count", "of"}
    if extra:
        return ClaimProblem("scope_invalid", pointer, f"{form} has no field {sorted(extra)[0]!r}")
    return ScopedClaim(form, pointer, statement, parsed, count, of)  # type: ignore[arg-type]


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _scope(value: Any) -> Scope | None:
    if not isinstance(value, Mapping) or not value or set(value) - {"query", "records"}:
        return None
    query, records = value.get("query"), value.get("records", [])
    if query is not None and (not isinstance(query, str) or not query):
        return None
    if not isinstance(records, list | tuple) or not all(
        isinstance(r, str) and r.startswith("record:") and "@" in r for r in records
    ):
        return None
    if query is None and not records:
        return None
    return Scope(() if query is None else (query,), tuple(records))


def scope_problem(
    claim: ScopedClaim, reads: frozenset[str], records: Mapping[str, str | None]
) -> ClaimProblem | None:
    """Why ``claim``'s scope is not one the instance read, or None when it is.

    ``reads`` are the instance's reads as text (``query:<id>@<hash>``, whole); ``records``
    maps a record ID to the version the log last recorded for it.
    """
    # A query ID may hold "@" in its arguments; the hash after the last "@" never does. So
    # the read's whole ID is compared, as invalidation compares it, never a prefix.
    read_queries = {r[len("query:") :].rpartition("@")[0] for r in reads if r.startswith("query:")}
    for query in claim.scope.queries:
        if query not in read_queries:
            return ClaimProblem(
                "scope_not_read", claim.pointer,
                f"{query!r} was never run here: run the search the claim rests on",
            )  # fmt: skip
    for read in claim.scope.records:
        if read not in reads:
            return ClaimProblem("scope_not_read", claim.pointer, f"{read} was never read here")
        rid, _, version = read[len("record:") :].rpartition("@")
        if rid in records and records[rid] != version:
            return ClaimProblem("scope_not_read", claim.pointer, f"{read} has moved; read it again")
    return None


def claims_in(content: Mapping[str, Any]) -> tuple[ScopedClaim, ...]:
    """The well-formed scoped claims of ``content`` (a refused one never saved)."""
    out = []
    for at, node in claim_nodes(content):
        found = parse_claim(node, at)
        if isinstance(found, ScopedClaim):
            out.append(found)
    return tuple(out)
