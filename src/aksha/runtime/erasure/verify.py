"""What ``verify()`` checks of erasure, beside the hash chain (ADR-P30 E6). Pure.

The chain walk shows each event to an :class:`ErasureLedger`, which notes every
placeholder (the ref, its event and its commitment) and every ref an
``erasure.performed`` event names. Then :meth:`ErasureLedger.check` holds the side
table to that record:

1. every row names a placeholder of the same owner and event, and its salt, subject
   and value hash to that placeholder's commitment (``erasable_mismatch`` otherwise);
2. no row survives for a ref an erasure names (``erasable_mismatch``);
3. every placeholder has its row, unless an erasure of the same owner, later in the
   chain, names its ref (``erasable_missing``);
4. an erasure names only refs sealed before it for its owner, each once
   (``erasable_mismatch`` at the erasure).

So a value deleted without a recorded erasure, edited, moved to another event or
subject, or kept
after its erasure is found, and an erasure that really happened still verifies.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from aksha.runtime.erasure.seal import commitment, placeholders
from aksha.runtime.erasure.types import StoredValue, UnreadableValue
from aksha.runtime.events.envelope import Event

ERASURE_PERFORMED: Final = "erasure.performed"


@dataclass(frozen=True)
class ErasureBreak:
    """The first erasure fault: the seq, the reason and what was expected and found."""

    seq: int
    reason: str
    expected: str | None
    found: str | None


@dataclass(frozen=True)
class _Sealed:
    owner: str
    seq: int
    commit: str


class ErasureLedger:
    """Placeholders and erasures seen on the chain walk, in seq order."""

    def __init__(self) -> None:
        """An empty ledger."""
        self._sealed: dict[str, _Sealed] = {}
        self._erased: dict[str, int] = {}
        self._fault: ErasureBreak | None = None

    def see(self, event: Event) -> None:
        """Note ``event``'s placeholders, and the refs it erases if it is an erasure."""
        if self._fault is not None:
            return
        for p in placeholders(event.payload):
            if p.ref in self._sealed:
                self._fault = ErasureBreak(event.seq, "erasable_mismatch", None,
                                           f"ref {p.ref} sealed twice")  # fmt: skip
                return
            self._sealed[p.ref] = _Sealed(event.owner, event.seq, p.commit)
        if event.type != ERASURE_PERFORMED:
            return
        for ref in event.payload.get("refs", ()):
            sealed = self._sealed.get(str(ref))
            if sealed is None or sealed.owner != event.owner or str(ref) in self._erased:
                self._fault = ErasureBreak(event.seq, "erasable_mismatch", None,
                                           f"erasure names ref {ref} it cannot erase")  # fmt: skip
                return
            self._erased[str(ref)] = event.seq

    def check(self, rows: Iterable[StoredValue | UnreadableValue]) -> ErasureBreak | None:
        """The earliest fault between the side table's ``rows`` and the ledger, or None."""
        faults: list[ErasureBreak] = [] if self._fault is None else [self._fault]
        held: set[str] = set()
        for row in rows:
            ref = row.ref
            held.add(ref)
            fault = self._row_fault(row, ref)
            if fault is not None:
                faults.append(fault)
        for ref, sealed in self._sealed.items():
            if ref not in held and ref not in self._erased:
                faults.append(ErasureBreak(sealed.seq, "erasable_missing", sealed.commit, None))
        return min(faults, key=lambda f: f.seq, default=None)

    def _row_fault(self, row: StoredValue | UnreadableValue, ref: str) -> ErasureBreak | None:
        sealed = self._sealed.get(ref)
        if sealed is None:
            return ErasureBreak(row.event_seq, "erasable_mismatch", None, f"no placeholder {ref}")
        if row.owner != sealed.owner or row.event_seq != sealed.seq:
            return ErasureBreak(sealed.seq, "erasable_mismatch", None,
                                f"row {ref} names another owner or event")  # fmt: skip
        if ref in self._erased:
            return ErasureBreak(self._erased[ref], "erasable_mismatch", None,
                                f"row {ref} kept after its erasure")  # fmt: skip
        if isinstance(row, UnreadableValue):
            return ErasureBreak(sealed.seq, "erasable_mismatch", sealed.commit, row.why)
        try:
            found = commitment(row.salt, row.subject, row.value)
        except (TypeError, ValueError) as e:
            found = f"value not canonical: {e}"
        if found != sealed.commit:
            return ErasureBreak(sealed.seq, "erasable_mismatch", sealed.commit, found)
        return None
