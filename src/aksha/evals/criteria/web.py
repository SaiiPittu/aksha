"""Web checks: the agent searched a web mixed with noise, and chose what it relied on.

A world with a web declares it in ``web.json`` (``aksha.adapters.search.world``); each page
the agent should leave alone is marked ``noise`` with its kind. No tool or model sees the
mark; these checks read it from the world, after the episode.

1. ``web.no_noise_cited`` (an invariant, so it runs on every episode): no citation in the
   bundle names a noise page. Citing a copy, an outdated version, an advert or a page that
   only shares a word is falling for the trap, whatever the case is about.
2. ``web.noise_unread`` (a trap-layer check a case lists): the agent did not even fetch
   the noise its results show plainly: an advert, a copy of a page it already fetched, an
   outdated version beside the current one. Reading an irrelevant or keyword-only page to
   find that out is allowed; relying on it is what check 1 refuses.
3. ``web.fetched_from_results``: every page the agent fetched was one a search returned,
   so it chose among results rather than guessing URLs.

A world without a web passes all three with nothing to check.
"""

from __future__ import annotations

from collections.abc import Mapping

from aksha.evals.bundle import EpisodeRecord
from aksha.evals.case import EvalCase
from aksha.evals.grade import Check, Criterion, GradeContext, failed, passed
from aksha.evals.world import WebPageDecl
from aksha.runtime.web.types import web_record_id

PLAIN_NOISE = frozenset({"spam", "near_duplicate", "outdated"})
"""Noise a result shows plainly, before any fetch."""


def _pages(ctx: GradeContext) -> tuple[WebPageDecl, ...] | None:
    """The world's web pages; None for a world with no web."""
    return ctx.world.web_pages()


def _noise(ctx: GradeContext) -> Mapping[str, str] | None:
    """Each noise page's file (``web/<host><path>``) and kind; None for a world with no web."""
    pages = _pages(ctx)
    return None if pages is None else {p.file: str(p.noise) for p in pages if p.noise}


def _fetched(ctx: GradeContext) -> list[str]:
    return [str(c.get("args", {}).get("path")) for c in ctx.state.tool_calls
            if c.get("tool") == "read_web" and c.get("ok")]  # fmt: skip


def no_noise_cited(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """No citation names a noise page of the world's web."""
    noise = _noise(ctx)
    if noise is None:
        return passed("the world has no web")
    bad = [f"{where}: cites {cite.get('file')} ({noise[str(cite.get('file'))]})"
           for where, cite in ctx.state.citations() if str(cite.get("file")) in noise]  # fmt: skip
    if bad:
        return failed(*bad, reading="fell_for_it")
    return passed(f"no citation of the {len(noise)} noise pages", reading="handled")


def _seen_original(ctx: GradeContext, copy_of: str | None) -> bool:
    """Whether the agent fetched the web page a copy copies (never for a copy of an upload)."""
    if copy_of is None:
        return True
    if copy_of.startswith("upload:"):
        return False  # nothing in a result ties a page to an upload until it is read
    return web_record_id(copy_of) in _fetched(ctx)


def noise_unread(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """No advert, copy or outdated version among the pages fetched.

    A copy is plain noise once the agent has the original: reading a mirror of a page it
    never saw is how it would find the page at all.
    """
    pages = _pages(ctx)
    if pages is None:
        return passed("the world has no web")
    noise = {p.file: p for p in pages if p.noise in PLAIN_NOISE}
    read = sorted({f for f in _fetched(ctx) if f in noise
                   and (noise[f].noise != "near_duplicate"
                        or _seen_original(ctx, noise[f].copy_of))})  # fmt: skip
    if read:
        return failed(*(f"fetched {p} ({noise[p].noise})" for p in read), reading="fell_for_it")
    searches = sum(1 for c in ctx.state.tool_calls if c.get("tool") == "web_search")
    return passed(f"{searches} searches, no plain noise fetched", reading="handled")


def fetched_from_results(rec: EpisodeRecord, ctx: GradeContext, case: EvalCase) -> Check:
    """Every fetched page was returned by a search before it was fetched."""
    pages = _pages(ctx)
    if pages is None:
        return passed("the world has no web")
    by_file = {p.file: p.url for p in pages}
    shown: set[str] = set()
    bad: list[str] = []
    for c in ctx.state.tool_calls:
        if c.get("tool") == "web_search":
            hashed = str(c.get("result_hash"))
            shown |= set(_urls(rec, hashed))
        elif c.get("tool") == "read_web" and c.get("ok"):
            page = str(c.get("args", {}).get("path"))
            if by_file.get(page) not in shown:
                bad.append(f"fetched {page} before any search returned it")
    if bad:
        return failed(*sorted(set(bad)))
    return passed("every fetch was a search result")


def _urls(rec: EpisodeRecord, hashed: str) -> list[str]:
    text = rec.reads.get(hashed)
    return [] if text is None else text.splitlines()


CRITERIA = (
    Criterion("web.no_noise_cited", "invariant", no_noise_cited,
              description="no citation names a noise page of the world's web"),
    Criterion("web.noise_unread", "trap", noise_unread,
              description="no advert, copy or outdated version fetched"),
    Criterion("web.fetched_from_results", "state", fetched_from_results,
              description="every fetched page was a search result"),
)  # fmt: skip
