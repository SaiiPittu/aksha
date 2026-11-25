"""Grounding refusals: ``MintRefused`` when a tool's span drafts cannot be minted.

A citation that fails verification is not an exception: the verifier returns a
``CiteError`` and the artifact store turns it into ``SaveRefused`` with the same code.
"""

from __future__ import annotations

from aksha.runtime.errors import AkshaError


class MintRefused(AkshaError):
    """Span drafts the runtime refuses to mint (D1).

    Code ``class_violation``: the tool does not declare ``mints_citation_spans``, a draft
    names a text its reader did not return in this attempt, or its offsets are not a
    non-empty range inside that text. The step records the code; nothing is minted.
    """

    default_code = "class_violation"
