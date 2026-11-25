"""Artifact store refusals: ``SaveRefused`` for the save path and the blob store's errors.

``SaveRefused`` carries a stable code and the JSON pointer of the failing part of the
request (``/content/rows/0``, ``/relations/1/evidence/0``), so the model sees what to fix.
"""

from __future__ import annotations

from aksha.runtime.errors import AkshaError


class SaveRefused(AkshaError):
    """A save the store refuses (ADR-P04 D7).

    Codes: ``kind_unknown``, ``rights``, ``key_required``, ``key_not_allowed``, ``schema``,
    ``not_canonical_content``, ``read_not_seen``, ``read_invalidated``,
    ``provenance_relation_forbidden``, ``relation_undeclared``, ``relation_label_invalid``,
    ``relation_end_invalid``, ``citation_missing``, ``citation_invalid``,
    ``foreign_writer``, and grounding's codes (ADR-P13): ``unknown_span``,
    ``span_not_seen``, ``stale_source``, ``quote_not_verbatim``, ``quote_too_short``,
    ``form_not_allowed``, ``superseded_statement``, ``stale_artifact_cite``,
    ``ungrounded_value``, ``cite_invalid``, ``span_not_of_source``, ``value_not_in_quote``,
    ``unknown_not_allowed``, ``unknown_invalid``.
    """

    default_code = "save_refused"

    def __init__(self, code: str, path: str, message: str) -> None:
        """Name the code, the pointer into the request (``""`` for all of it) and why."""
        self.path = path
        super().__init__(code, f"{path or '/'}: {message}")


class BlobError(AkshaError):
    """A blob the store cannot give or take.

    Codes: ``blob_ref_invalid`` (not ``sha256:`` and 64 hex characters), ``blob_not_found``,
    ``blob_corrupt`` (the stored bytes no longer hash to their name) and
    ``blob_store_required`` (a payload holds a stub and no blob store was given).
    """

    default_code = "blob_error"
