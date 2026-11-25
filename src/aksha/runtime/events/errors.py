"""Refusals of the event model: unknown types and boundaries, bad payloads, bad dedup keys.

``CatalogueError`` is a mistake in a declared event type, found when the catalogue is
built or an extension registers a type. The others refuse one event on append.
"""

from __future__ import annotations

from aksha.runtime.errors import AkshaError


class CatalogueError(AkshaError, ValueError):
    """A declared event type is malformed, duplicated or claims a core name."""

    default_code = "catalogue_invalid"


class UnknownEventType(AkshaError):
    """The catalogue holds no event type of this name (ADR-P01 D6: an error on append only)."""

    default_code = "unknown_event_type"


class UnknownBoundary(AkshaError):
    """The transaction boundary is not in the closed list."""

    default_code = "unknown_boundary"


class BoundaryNotAllowed(AkshaError):
    """The event type may not appear in this transaction boundary."""

    default_code = "boundary_not_allowed"


class PayloadInvalid(AkshaError):
    """A payload fails its JSON Schema; ``path`` names the failing field, e.g. ``$.text``."""

    default_code = "payload_invalid"

    def __init__(self, event_type: str, path: str, reason: str) -> None:
        """Name the event type, the failing JSON path and the schema's reason."""
        self.event_type = event_type
        self.path = path
        super().__init__(message=f"{event_type} payload at {path}: {reason}")


class PayloadTooLarge(AkshaError):
    """A payload's canonical JSON is over the cap (ADR-P01 D7); store the body as a blob."""

    default_code = "payload_too_large"

    def __init__(self, event_type: str, size: int, cap: int) -> None:
        """Name the event type, the payload's size in bytes and the cap."""
        self.event_type = event_type
        self.size = size
        super().__init__(message=f"{event_type} payload is {size} bytes, over the {cap} cap")


class DedupKeyError(AkshaError):
    """A dedup key is missing a field, disagrees with its template, or repeats in a boundary.

    Codes: ``dedup_key_field_missing``, ``dedup_key_mismatch``, ``dedup_key_not_allowed``,
    ``dedup_key_repeated_in_boundary``.
    """

    default_code = "dedup_key_error"
