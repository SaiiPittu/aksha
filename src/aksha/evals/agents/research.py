"""The research agent's eval adapter: its runtime run, saved into the episode's bundle schema.

The agent runs as it ships (``aksha.agents.research``, its rule-based fake model, its own
tools reading through :class:`~aksha.evals.agents.runtime.EpisodeReader`). The scripted user
answers its scope question (``geography``) with ``EU``. When the run ends, its artifacts
are written into the episode in the schema of ``docs/evals/README.md``, every citation
being a span its tools minted (file, exact text and code-point offsets):

=======================  ===================================================================
Bundle output            From the run's artifacts
=======================  ===================================================================
``competitor/E<n>``      ``competitors`` (name, aliases) and the ``matrix`` row: unit prices
                         (``unit_price``; a price the pages do not give is ``amount: null``,
                         label ``unknown``, cited by the page's ``price_notes``) and battery
                         claims and facts (``battery_life``). ``E<n>`` numbers the companies
                         by their first page, so a renamed world keeps each key
``same_entity_as``       each company's ``merge_evidence``, cited by its span
``contradicts``          a row with ``price_conflict``, cited by its later price's span
``review_summary/all``   ``needs.sample`` (rows, unique, duplicate pairs) and each need's
                         theme count over the unique reviews
``flag``                 each ``instruction-like`` finding, with the text it quotes
=======================  ===================================================================

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
SOURCE = "corpus"
ANSWERS: Mapping[str, str] = {"segment": "offices", "geography": "EU"}
"""The scripted user's answers to the agent's scope questions, by question ID."""


class ResearchUnderTest(RuntimeUnderTest):
    """The research agent through an episode (module doc)."""

    name = AGENT
    agent = AGENT
    sources = (SOURCE,)
    request = "Who sells camera-free occupancy sensing for offices, and at what price?"

    def answer(self, w: Waiting, question: str, text: str) -> str:
        """The scope answer by question ID; any other question is refused."""
        if question in ANSWERS:
            return ANSWERS[question]
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
            key=lambda c: min((str(p.get("record")) for p in c.get("pages", ())), default=""),
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
        prices = [_price(spans, p, notes) for p in row.get("unit_price", ())]
        claims = [
            {"text": c.get("value"), "label": c.get("label"), "citation": cite}
            for c in row.get("battery_life", ())
            if c.get("label") in ("vendor_claim", "fact")
            for cite in [_cite(spans, c.get("cite"))]
            if cite is not None
        ]
        merge = [
            (str(m.get("page", "")), cite)
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
                "pages": [p.get("record") for p in company.get("pages", ())],
                "prices": prices,
                "claims": claims,
            },
            [*own, *(c for _, c in merge)],
        )
        pages = [str(p.get("record")) for p in company.get("pages", ())]
        for page, cite in merge:
            first = next((p for p in pages if p != page), pages[0] if pages else page)
            ep.relate("same_entity_as", f"page:{first}", f"page:{page}", cite)
        if row.get("price_conflict"):
            stated = [p for p in prices if p["amount"] is not None and p.get("citation")]
            if len(stated) >= 2:
                ep.relate(
                    "contradicts",
                    f"price:{stated[0]['citation']['file']}",
                    f"price:{stated[-1]['citation']['file']}",
                    stated[-1]["citation"],
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
        ep.save(
            "review_summary",
            "all",
            {
                "rows": sample.get("rows"),
                "reviews_unique": sample.get("unique"),
                "duplicates": [[d.get("review"), d.get("duplicate_of")] for d in duplicates],
                "themes": themes,
                "statements": [
                    f"{x.get('phrase')} mention {x.get('need', x.get('theme'))}."
                    for x in needs.get("needs", ())
                    if x.get("phrase")
                ],
            },
            cites,
        )


def _cite(spans: SpanIndex, cite: object) -> dict[str, Any] | None:
    """The bundle citation of an artifact's ``{quote, span}`` cite, from its minted span."""
    if not isinstance(cite, Mapping):
        return None
    span = spans.get(str(cite.get("span", "")))
    if span is None:
        return None
    return {"file": span.record_id, "exact": span.text, "start": span.start, "end": span.end}


def _price(
    spans: SpanIndex, entry: Mapping[str, Any], notes: list[dict[str, Any]]
) -> dict[str, Any]:
    """One unit price as the bundle schema writes it; an unstated one is null, never 0.

    An unstated price is cited by the page's own note on pricing (the profile's
    ``price_notes``), when the agent recorded one.
    """
    value = entry.get("value")
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
