"""The values of erasure (ADR-P30 amendments E1 to E10): the marker, the sealed value, the receipt.

A writer marks a personal value in an event payload with :class:`Personal`. On append the
store moves it into the ``erasables`` side table as a :class:`SealedValue`, and the
event's hashed payload keeps only a placeholder: the value's ref, a salted commitment
to it and the stand-in readers see once it is erased. Erasing a subject deletes its
sealed values and records ``erasure.performed``; :class:`ErasureReceipt` is what the
caller gets back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from aksha.runtime.storage.errors import StoreError

REDACTED: Final = "[erased]"
"""The default stand-in a reader sees in place of an erased value (E2)."""

SUBJECT_MAX: Final = 200
"""The longest subject id, in characters."""


class ErasureError(StoreError):
    """A refusal by erasure: a payload it cannot seal cleanly, or a bad erase request.

    Codes: ``erasure_reserved_key`` (a caller payload uses the placeholder key),
    ``erasure_nested`` (a :class:`Personal` inside another), ``erasure_subject_invalid``,
    ``erasure_value_invalid`` (a value or stand-in outside the chain's encoding),
    ``erasure_in_dedup_key`` (a dedup key that reads a personal value),
    ``erasure_in_artifact`` (personal values in artifact content, not supported, E8) and
    ``erasure_reason_invalid``.
    """

    default_code = "erasure_error"


@dataclass(frozen=True)
class Personal:
    """A personal value inside a payload, about ``subject``, that can later be erased.

    ``subject`` is an opaque id the caller chooses (``person:p07``), never a name: it
    stays in the record after erasure, so it must not itself identify the person.
    ``redacted`` is what every reader sees once the value is erased, and the payload with
    it in place must still fit the event's schema, so replay after erasure is valid (E2).
    """

    subject: str
    value: Any
    redacted: Any = REDACTED


@dataclass(frozen=True)
class SealedValue:
    """One personal value as the side table keeps it (E1).

    ``ref`` is ``er_`` plus 32 random hex characters. ``commit`` is ``sha256:`` over the
    canonical bytes of ``{"salt": salt, "subject": subject, "value": value}``: the event's
    hash covers it, so an edited value or subject breaks the chain, and the random salt (deleted with the value) means
    a short value such as a name cannot be guessed back from the commitment.
    """

    ref: str
    subject: str
    salt: str
    value: Any
    commit: str


@dataclass(frozen=True)
class StoredValue:
    """A side-table row as read back: its owner and the seq of the event that sealed it."""

    owner: str
    event_seq: int
    ref: str
    subject: str
    salt: str
    value: Any


@dataclass(frozen=True)
class UnreadableValue:
    """A side-table row whose value could not be decoded, or not canonically."""

    owner: str
    ref: str
    event_seq: int
    why: str


@dataclass(frozen=True)
class Placeholder:
    """What the hashed payload holds in place of one personal value."""

    ref: str
    commit: str
    redacted: Any


@dataclass(frozen=True)
class ErasureReceipt:
    """The outcome of erasing one subject for one owner.

    ``refs`` are the values erased by this call, ordered by the seq of the event that
    sealed each, then by ref; ``seq``
    is the ``erasure.performed`` event's. Nothing left to erase gives ``refs == ()`` and
    ``seq is None``, and records nothing (E5).
    """

    owner: str
    subject: str
    refs: tuple[str, ...]
    seq: int | None
