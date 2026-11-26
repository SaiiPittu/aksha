"""Grounding and citations (ADR-P13): a claim points at text a tool returned, or is unknown.

Built: ``types.py`` (span drafts, citation spans, the three cite forms, ``Unknown``,
``CitePolicy``), ``errors.py`` (``MintRefused``), ``spans.py`` (minting with S1's shown
set, statement spans, ``excerpts.shown``, the span index), ``verify.py`` (the one
verifier and the content walk) and ``facts.py`` (``citation.bound`` and the evidence a
save reads). The artifact store's save path calls the verifier on every save, for content
and for every typed relation's evidence; no extension can switch it off.

Planned: the fast-model verifier that only demotes (D6, with model routing), the
provider-citation adapter (D7), minting from ``tree`` and ``exec`` sources, and fuzzy
re-anchoring for display.
"""
