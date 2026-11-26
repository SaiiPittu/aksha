"""Sealing personal values out of a payload, and resolving them back (ADR-P30 amendments E1-E4)."""

from __future__ import annotations

import secrets

import pytest

from aksha.runtime.canonical import canonical_bytes, sha256_ref
from aksha.runtime.erasure.seal import (
    PLACEHOLDER_KEY,
    commitment,
    is_placeholder,
    placeholders,
    redacted,
    resolve,
    seal,
    unsealed,
)
from aksha.runtime.erasure.types import REDACTED, ErasureError, Personal


def _payload() -> dict[str, object]:
    return {
        "n": 1,
        "who": Personal("person:p1", "A. Contributor"),
        "list": [Personal("person:p2", {"employer": "Example Ltd"}, redacted=None), "plain"],
    }


def test_seal_moves_each_personal_value_behind_a_placeholder() -> None:
    sealed, values = seal(_payload(), entropy=secrets.token_hex)
    assert [v.subject for v in values] == ["person:p1", "person:p2"]
    text = canonical_bytes(sealed).decode()
    assert "A. Contributor" not in text and "Example Ltd" not in text
    who = sealed["who"]
    assert is_placeholder(who)
    assert who[PLACEHOLDER_KEY] == values[0].ref and who["redacted"] == REDACTED
    assert who["commit"] == values[0].commit
    assert sealed["list"][1] == "plain" and sealed["n"] == 1


def test_the_commitment_is_salted_so_two_seals_of_one_value_differ() -> None:
    first, a = seal({"who": Personal("person:p1", "Same Name")}, entropy=secrets.token_hex)
    second, b = seal({"who": Personal("person:p1", "Same Name")}, entropy=secrets.token_hex)
    assert a[0].ref != b[0].ref and a[0].commit != b[0].commit
    assert a[0].commit == sha256_ref(canonical_bytes({"salt": a[0].salt, "subject": "person:p1", "value": "Same Name"}))
    assert commitment(a[0].salt, a[0].subject, "Same Name") == a[0].commit
    assert first != second


def test_resolve_returns_each_value_or_its_redacted_stand_in() -> None:
    sealed, values = seal(_payload(), entropy=secrets.token_hex)
    full = resolve(sealed, {v.ref: v.value for v in values})
    assert full == unsealed(_payload())
    gone = resolve(sealed, {values[1].ref: values[1].value})
    assert gone["who"] == REDACTED and gone["list"][0] == {"employer": "Example Ltd"}
    assert resolve(sealed, {}) == redacted(_payload())


def test_placeholders_lists_refs_in_payload_order() -> None:
    sealed, values = seal(_payload(), entropy=secrets.token_hex)
    assert [(p.ref, p.commit) for p in placeholders(sealed)] == [(v.ref, v.commit) for v in values]
    assert placeholders({"x": [1, {"y": "z"}]}) == ()


def test_a_payload_without_personal_values_is_returned_unchanged() -> None:
    payload = {"a": [1, {"b": "c"}]}
    sealed, values = seal(payload, entropy=secrets.token_hex)
    assert sealed == payload and values == ()


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"x": {PLACEHOLDER_KEY: "er_0"}}, "erasure_reserved_key"),
        ({"x": Personal("s", Personal("s", "v"))}, "erasure_nested"),
        ({"x": Personal("", "v")}, "erasure_subject_invalid"),
        ({"x": Personal("s\x00", "v")}, "erasure_subject_invalid"),
        ({"x": Personal("s" * 201, "v")}, "erasure_subject_invalid"),
        ({"x": Personal("s", 1.5)}, "erasure_value_invalid"),
        ({"x": Personal("s", "v", redacted=2.5)}, "erasure_value_invalid"),
    ],
)
def test_seal_refuses_what_it_cannot_erase_cleanly(payload: dict[str, object], code: str) -> None:
    with pytest.raises(ErasureError) as e:
        seal(payload, entropy=secrets.token_hex)
    assert e.value.code == code


def test_seal_takes_its_randomness_from_the_injected_entropy() -> None:
    draws = iter(["aa" * 32, "bb" * 16])
    sealed, (value,) = seal({"who": Personal("person:p1", "N")}, entropy=lambda n: next(draws))
    assert value.salt == "aa" * 32 and value.ref == "er_" + "bb" * 16
    assert sealed["who"][PLACEHOLDER_KEY] == value.ref


def test_seal_without_entropy_refuses_a_personal_value() -> None:
    assert seal({"a": 1}) == ({"a": 1}, ())
    with pytest.raises(ErasureError) as e:
        seal({"who": Personal("person:p1", "N")})
    assert e.value.code == "erasure_no_entropy"
