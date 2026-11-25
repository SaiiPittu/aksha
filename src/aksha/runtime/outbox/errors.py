"""Refusals of the outbox (ADR-P03), each with a stable code.

``OutboxRefused``: authority does not cover the entry; its code is the reason
(``approval_required``, ``hash_mismatch``, ``target_invalidated``). ``KeyFieldMissing``
(``key_field_missing``): a declared key field is absent from the arguments.
``SystemUnknown`` (``system_unknown``): an entry names a system no adapter is registered
for. ``TransientEffectError`` (``transient`` by default): what a system of record raises
for a failure worth retrying.
"""

from __future__ import annotations

from aksha.runtime.errors import AkshaError


class OutboxRefused(AkshaError):
    """The entry is refused before it is written or run; ``code`` says why."""

    default_code = "approval_required"


class KeyFieldMissing(AkshaError):
    """An argument named by ``key_fields`` is missing, so the effect has no key."""

    default_code = "key_field_missing"


class SystemUnknown(AkshaError):
    """No system of record is registered under the entry's target name."""

    default_code = "system_unknown"


class TransientEffectError(AkshaError):
    """Raised by a system of record for a retryable failure; ``code`` is matched to ``retry_on``."""

    default_code = "transient"
