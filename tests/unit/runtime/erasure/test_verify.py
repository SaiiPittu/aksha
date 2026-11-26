"""What ``verify()`` holds erasure to, beside the hash chain (ADR-P30 E6, check 4 and refs).

The contract suite covers a value deleted, edited, moved or kept after its erasure. These
cover the erasure event itself: one that names a ref it cannot erase is a break at the
erasure, and a ref sealed twice is a break where it repeats.
"""

from __future__ import annotations

import secrets
from typing import Any

from aksha.runtime.erasure.seal import seal
from aksha.runtime.erasure.types import Personal, StoredValue
from aksha.runtime.erasure.verify import ERASURE_PERFORMED, ErasureLedger
from aksha.runtime.events.envelope import Event


def _event(seq: int, owner: str, type_: str, payload: dict[str, Any]) -> Event:
    return Event(type_, payload, seq=seq, owner=owner, at="2026-11-26T00:00:00Z", boot=1,
                 schema_version=1)  # fmt: skip


def _sealed(seq: int, owner: str = "o1") -> tuple[Event, StoredValue]:
    payload, (value,) = seal({"text": Personal("person:p1", "N")}, entropy=secrets.token_hex)
    row = StoredValue(owner, seq, value.ref, value.subject, value.salt, value.value)
    return _event(seq, owner, "probe.noted", payload), row


def _erasure(seq: int, owner: str, *refs: str) -> Event:
    payload = {"subject": "person:p1", "refs": list(refs), "reason": "r"}
    return _event(seq, owner, ERASURE_PERFORMED, payload)


def test_an_erasure_of_its_own_owners_ref_verifies() -> None:
    sealed, row = _sealed(1)
    ledger = ErasureLedger()
    ledger.see(sealed)
    ledger.see(_erasure(2, "o1", row.ref))
    assert ledger.check([]) is None


def test_an_erasure_naming_another_owners_ref_is_a_break_at_the_erasure() -> None:
    sealed, row = _sealed(1, owner="o2")
    ledger = ErasureLedger()
    ledger.see(sealed)
    ledger.see(_erasure(2, "o1", row.ref))
    found = ledger.check([row])
    assert found is not None and (found.seq, found.reason) == (2, "erasable_mismatch")


def test_an_erasure_naming_an_unknown_or_later_ref_is_a_break() -> None:
    sealed, row = _sealed(2)
    ledger = ErasureLedger()
    ledger.see(_erasure(1, "o1", row.ref))  # names a ref not sealed yet
    ledger.see(sealed)
    found = ledger.check([row])
    assert found is not None and (found.seq, found.reason) == (1, "erasable_mismatch")


def test_a_ref_erased_twice_is_a_break_at_the_second_erasure() -> None:
    sealed, row = _sealed(1)
    ledger = ErasureLedger()
    ledger.see(sealed)
    ledger.see(_erasure(2, "o1", row.ref))
    ledger.see(_erasure(3, "o1", row.ref))
    found = ledger.check([])
    assert found is not None and (found.seq, found.reason) == (3, "erasable_mismatch")


def test_a_ref_sealed_twice_is_a_break_where_it_repeats() -> None:
    sealed, row = _sealed(1)
    again = _event(2, "o1", "probe.noted", dict(sealed.payload))
    ledger = ErasureLedger()
    ledger.see(sealed)
    ledger.see(again)
    found = ledger.check([row])
    assert found is not None and (found.seq, found.reason) == (2, "erasable_mismatch")


def test_a_row_with_no_placeholder_is_a_break() -> None:
    _, row = _sealed(1)
    found = ErasureLedger().check([row])
    assert found is not None and (found.seq, found.reason) == (1, "erasable_mismatch")


def test_a_row_moved_to_another_subject_is_a_mismatch() -> None:
    sealed, row = _sealed(1)
    ledger = ErasureLedger()
    ledger.see(sealed)
    moved = StoredValue(row.owner, row.event_seq, row.ref, "person:p2", row.salt, row.value)
    fault = ledger.check([moved])
    assert fault is not None and fault.reason == "erasable_mismatch" and fault.seq == 1
    assert ErasureLedger().check([]) is None
