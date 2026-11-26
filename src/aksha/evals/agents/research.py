"""The research agent's eval adapter: its runtime run, saved into the episode's bundle schema.

The agent runs as it ships (``aksha.agents.research``, its rule-based fake model, its own
tools reading through :class:`~aksha.evals.agents.runtime.EpisodeReader`). The scripted user
answers its scope questions (``segment``, ``geography``) with the world's own answers.
When the run ends, its artifacts are written into the episode in the schema of
``docs/evals/README.md``, every citation being a span its tools minted (file, exact text
and code-point offsets):

=======================  ===================================================================
Bundle output            From the run's artifacts
=======================  ===================================================================
``competitor/E<n>``      ``competitors`` (name, aliases) and the ``matrix`` row: its prices
                         (``prices``; a price the pages do not give is ``amount: null``,
                         label ``unknown``, cited by the page's ``price_notes``) and every
                         dimension's vendor claims and facts (``cells``). ``E<n>`` numbers
                         the companies by their first page, so a renamed world keeps each key
``same_entity_as``       each company's ``merge_evidence``, cited by its span
``contradicts``          a row with ``price_conflict``, cited by its later price's span
``review_summary/all``   ``needs.sample`` (rows, unique, duplicate pairs) and each need's
                         theme count over the unique reviews
``flag``                 each ``instruction-like`` finding, with the text it quotes
``scope/all``            the ``scope`` artifact's geography (its words, the regions they name
                         and where they came from, cited when a note gave them) and segment;
                         saved last, so a change step can follow a finished export
=======================  ===================================================================

``review_summary/all`` also carries each theme's count of unique reviews inside the
scope's geography (``themes_in_scope``, the ``needs`` artifact's ``in_scope_geography``).

The adapter computes nothing a grader checks: it renames fields and resolves span IDs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.evals.agents.runtime import EpisodeReader, RuntimeUnderTest
from aksha.evals.episode import Episode
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.versions import current
from aksha.runtime.grounding.spans import span_index
from aksha.runtime.grounding.types import SpanIndex
from aksha.runtime.projections.view import LogView
from aksha.runtime.session.views import Waiting

AGENT = "research"
SOURCE = "inputs"
COLLECTIONS = ("input",)
"""The one collection of ``inputs``, which a change set may move."""
PREFIX = "input/"
"""Every input's record ID starts with its collection; a bundle names the world's file."""
ANSWERS: Mapping[str, Mapping[str, str]] = {
    "stillroom": {"segment": "offices", "geography": "EU"},
    "stillroom-v2": {"segment": "offices", "geography": "EU"},
    "tilldew": {"segment": "smallholder vegetable farmers", "geography": "India"},
}
"""The scripted user's answers to the agent's scope questions, per world, by question ID."""


class ResearchUnderTest(RuntimeUnderTest):
    """The research agent through an episode (module doc)."""

    name = AGENT
    agent = AGENT
    sources = (SOURCE,)
    collections = COLLECTIONS
    request = (
        "Who sells camera-free occupancy sensing for offices, and at what price? Compare power "
        "(PoE, Ethernet, mains, USB-C, battery powered, battery thermal, AA), battery life "
        "(battery life, years of battery, months at) and privacy (privacy, camera, faces, "
        "identities, image)."
    )
    """The opening when a case gives none: what to compare is part of the request."""

    world = ""

    def run(self, episode: Episode) -> None:
        """Remember the episode's world (the scripted user's answers depend on it), then run."""
        self.world = episode.world
        super().run(episode)

    def answer(self, w: Waiting, question: str, text: str) -> str:
        """The scope answer by question ID; any other question is refused."""
        given = ANSWERS.get(self.world, ANSWERS["stillroom"])
        if question in given:
            return given[question]
        return super().answer(w, question, text)

    def export(
        self, view: LogView, episode: Episode, reader: EpisodeReader, blobs: BlobStore | None
    ) -> None:
        """Competitors, their edges, the review summary and the flags (module doc)."""
        spans = span_index(view)

        def content(kind: str) -> Mapping[str, Any]:
            found = current(view, f"{AGENT}.{kind}", None, blobs=blobs)
            return {} if found is None else found.content

        def profile(key: str) -> Mapping[str, Any]:
            found = current(view, f"{AGENT}.profile", key, blobs=blobs)
            return {} if found is None else found.content

        rows = {str(r.get("key")): r for r in content("matrix").get("rows", ())}
        companies = sorted(
            content("competitors").get("companies", ()),
            key=lambda c: min((_file(p.get("record")) for p in c.get("pages", ())), default=""),
        )
        for i, company in enumerate(companies, start=1):
            key = str(company.get("key"))
            self._company(episode, spans, f"E{i}", company, rows.get(key, {}), profile(key))
        self._reviews(episode, spans, content("needs"))
        for finding in content("findings").get("findings", ()):
            if str(finding.get("id", "")).startswith("instruction-like"):
                for e in finding.get("evidence", ()):
                    c = _cite(spans, e.get("cite"))
                    if c is not None:
                        episode.flag("instruction_like", c["exact"], c["file"])
        self._scope(episode, spans, content("scope"))

    def _scope(self, ep: Episode, spans: SpanIndex, scope: Mapping[str, Any]) -> None:
        if not scope:
            return
        geo = scope.get("geography", {})
        region = geo.get("normalised")
        cite = _cite(spans, geo.get("cite"))
        ep.save(
            "scope",
            "all",
            {
                "geography": geo.get("value"),
                "regions": [] if region is None else str(region).split("+"),
                "basis": geo.get("basis"),
                "segment": scope.get("segment", {}).get("value"),
            },
            [] if cite is None else [cite],
        )

    def _company(
        self,
        ep: Episode,
        spans: SpanIndex,
        key: str,
        company: Mapping[str, Any],
        row: Mapping[str, Any],
        profile: Mapping[str, Any],
    ) -> None:
        canonical = company.get("canonical", {})
        notes = [
            c
            for n in profile.get("price_notes", ())
            for c in (_cite(spans, n.get("cite")),)
            if c is not None
        ]
        prices = [_price(spans, p, notes) for p in row.get("prices", ())]
        claims = [
            {"text": c.get("value"), "label": c.get("label"), "citation": cite}
            for entries in dict(row.get("cells", {})).values()
            for c in entries
            if c.get("label") in ("vendor_claim", "fact")
            for cite in [_cite(spans, c.get("cite"))]
            if cite is not None
        ]
        merge = [
            (_file(m.get("page", "")), cite)
            for m in company.get("merge_evidence", ())
            for cite in [_cite(spans, m.get("cite"))]
            if cite is not None
        ]
        own = [c for c in (_cite(spans, canonical.get("cite")),) if c is not None]
        ep.save(
            "competitor",
            key,
            {
                "name": canonical.get("value"),
                "aliases": [a.get("value") for a in company.get("aliases", ())],
                "pages": [_file(p.get("record")) for p in company.get("pages", ())],
                "prices": prices,
                "claims": claims,
            },
            [*own, *(c for _, c in merge)],
        )
        pages = [_file(p.get("record")) for p in company.get("pages", ())]
        for page, cite in merge:
            first = next((p for p in pages if p != page), pages[0] if pages else page)
            ep.relate("same_entity_as", f"page:{first}", f"page:{page}", cite)
        if row.get("price_conflict"):
            stated = [p for p in prices if p["amount"] is not None and p.get("citation")]
            by_basis: dict[str, list[dict[str, Any]]] = {}
            for p in stated:
                by_basis.setdefault(" ".join(str(p.get("per")).lower().split()), []).append(p)
            for on in by_basis.values():
                if len({str(p["amount"]) for p in on}) < 2:
                    continue
                ep.relate(
                    "contradicts",
                    f"price:{on[0]['citation']['file']}",
                    f"price:{on[-1]['citation']['file']}",
                    on[-1]["citation"],
                    label="price conflict left open",
                )

    def _reviews(self, ep: Episode, spans: SpanIndex, needs: Mapping[str, Any]) -> None:
        sample = needs.get("sample")
        if not isinstance(sample, Mapping):
            return
        duplicates = sample.get("duplicates", ())
        cites = [
            c
            for d in duplicates
            for e in d.get("evidence", ())
            for c in (_cite(spans, e.get("cite")),)
            if c is not None
        ]
        themes = {str(x.get("theme")): x.get("count") for x in needs.get("needs", ())}
        in_scope = {
            str(x.get("theme")): x.get("in_scope_geography", {}).get("count")
            for x in needs.get("needs", ())
        }
        ep.save(
            "review_summary",
            "all",
            {
                "rows": sample.get("rows"),
                "reviews_unique": sample.get("unique"),
                "duplicates": [[d.get("review"), d.get("duplicate_of")] for d in duplicates],
                "themes": themes,
                "themes_in_scope": in_scope,
                "statements": [
                    f"{x.get('phrase')} mention {x.get('need', x.get('theme'))}."
                    for x in needs.get("needs", ())
                    if x.get("phrase")
                ],
            },
            cites,
        )


def _file(record: object) -> str:
    """The world's file path of an input record ID (``input/<path>``)."""
    return str(record).removeprefix(PREFIX)


def _cite(spans: SpanIndex, cite: object) -> dict[str, Any] | None:
    """The bundle citation of an artifact's ``{quote, span}`` cite, from its minted span."""
    if not isinstance(cite, Mapping):
        return None
    span = spans.get(str(cite.get("span", "")))
    if span is None:
        return None
    return {"file": _file(span.record_id), "exact": span.text, "start": span.start,
            "end": span.end}  # fmt: skip


def _price(
    spans: SpanIndex, entry: Mapping[str, Any], notes: list[dict[str, Any]]
) -> dict[str, Any]:
    """One unit price as the bundle schema writes it; an unstated one is null, never 0.

    An unstated price is cited by the page's own note on pricing (the profile's
    ``price_notes``), when the agent recorded one.
    """
    value = entry.get("number", entry.get("value"))
    if entry.get("status") == "unknown" or value is None:
        return {"amount": None, "currency": None, "per": None, "label": "unknown",
                "note": str(entry.get("question", "")),
                "citation": notes[0] if notes else None}  # fmt: skip
    return {
        "amount": int(value) if str(value).isdigit() else value,
        "currency": entry.get("currency"),
        "per": entry.get("per"),
        "page_published": entry.get("published"),
        "label": "stated",
        "terms": entry.get("terms"),
        "citation": _cite(spans, entry.get("cite")),
    }
