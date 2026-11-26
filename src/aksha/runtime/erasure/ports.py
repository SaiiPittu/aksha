"""The erasure hook (ADR-P30 E7): the one call another primitive makes to erase a subject.

Memory (ADR-P09) calls :meth:`Eraser.erase` when a ``forget`` must be a real erasure
rather than a tombstone that hides from recall. Both storage adapters implement it; the
composition root hands the writer's event store to whoever needs it.
"""

from __future__ import annotations

from typing import Protocol

from aksha.runtime.erasure.types import ErasureReceipt


class Entropy(Protocol):
    """Random hex for a salt or a ref: ``nbytes`` random bytes as ``2 * nbytes`` hex characters.

    Injected, so the runtime core reads no ambient randomness (ADR-P02 D3). Both storage
    adapters pass ``secrets.token_hex``; a test may pass a fixed sequence.
    """

    def __call__(self, nbytes: int, /) -> str:
        """``nbytes`` random bytes, hex-encoded."""
        ...


class Eraser(Protocol):
    """Erases every personal value of one subject for one owner, and records that it did."""

    def erase(self, owner: str, subject: str, *, reason: str) -> ErasureReceipt:
        """Delete the owner's sealed values for ``subject`` and record ``erasure.performed``.

        One transaction: the deletes and the event commit together or not at all. Writer
        only (``NotWriter``). Idempotent: with nothing left to erase it records nothing
        and returns ``refs == ()``. ``reason`` is operator text, kept in the record, so it
        must not name the person either.
        """
        ...
