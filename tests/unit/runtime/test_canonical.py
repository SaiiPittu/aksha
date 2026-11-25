import hashlib
from datetime import UTC, datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from aksha.runtime.canonical import canonical_json, freeze, iso_utc, sha256_hex, thaw


def test_canonical_json_is_sorted_and_compact() -> None:
    assert canonical_json({"b": 1, "a": [1, "é"]}) == '{"a":[1,"é"],"b":1}'


def test_canonical_json_accepts_frozen_values() -> None:
    assert canonical_json(freeze({"a": [1, {"b": 2}]})) == '{"a":[1,{"b":2}]}'


def test_canonical_json_refuses_nan_and_unknown_types() -> None:
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})
    with pytest.raises(TypeError):
        canonical_json({"x": object()})


def test_sha256_hex_hashes_the_canonical_form() -> None:
    expected = hashlib.sha256(b'{"a":1,"b":2}').hexdigest()
    assert sha256_hex({"b": 2, "a": 1}) == expected


def test_freeze_and_thaw_round_trip() -> None:
    value = {"a": [1, {"b": 2}], "c": "d"}
    frozen = freeze(value)
    assert isinstance(frozen, MappingProxyType)
    assert frozen["a"] == (1, MappingProxyType({"b": 2}))
    assert thaw(frozen) == value


def test_iso_utc_formats_and_converts() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    assert iso_utc(datetime(2026, 11, 27, 14, 30, tzinfo=ist)) == "2026-11-27T09:00:00.000000Z"
    assert iso_utc(datetime(2026, 11, 27, tzinfo=UTC)) == "2026-11-27T00:00:00.000000Z"
    with pytest.raises(ValueError):
        iso_utc(datetime(2026, 11, 27))


def test_nul_path_names_the_first_string_holding_nul() -> None:
    from aksha.runtime.canonical import nul_path

    assert nul_path({"a": "ok", "b": [1, "x"]}) is None
    assert nul_path({"a": {"b": ["ok", "x\x00"]}}) == "$.a.b[1]"
    assert nul_path("\x00") == "$"
    assert nul_path({"k\x00": 1}) is not None


# --- The chain's encoding: RFC 8785 (JCS) subset, ADR-P01 Amendment H, H3 -----------------


def test_canonical_bytes_matches_rfc8785_vectors() -> None:
    from aksha.runtime.canonical import canonical_bytes

    # RFC 8785 section 3.2.3: keys sort by UTF-16 code units, so the emoji (a surrogate
    # pair starting 0xD83D) sorts before U+FB33, unlike code-point order.
    keys = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }
    order = ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]
    expected = "{" + ",".join(f'"{k}":"{keys[k]}"' for k in order) + "}"
    expected = expected.replace("\r", "\\r")
    assert canonical_bytes(keys) == expected.encode("utf-8")
    # RFC 8785 section 3.2.2.2: minimal string escaping, lowercase hex for controls.
    assert canonical_bytes('€$\u000f\nA\'B"\\\\"/') == '"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"'.encode()
    assert canonical_bytes([None, True, False, 0, -1, 2**53, "", {}, []]) == (
        b'[null,true,false,0,-1,9007199254740992,"",{},[]]'
    )


def test_canonical_bytes_accepts_frozen_values() -> None:
    from aksha.runtime.canonical import canonical_bytes

    assert canonical_bytes(freeze({"b": [1, {"c": "é"}], "a": None})) == (
        '{"a":null,"b":[1,{"c":"é"}]}'.encode()
    )


@pytest.mark.parametrize(
    ("value", "path"),
    [
        ({"a": 1.5}, "$.a"),
        ({"a": [1, 2.0]}, "$.a[1]"),
        ({"a": float("nan")}, "$.a"),
        ({"big": 2**53 + 1}, "$.big"),
        ({"small": -(2**53) - 1}, "$.small"),
        ({"k": {1: "x"}}, "$.k"),
        ({"s": "\ud800"}, "$.s"),
        ({"o": object()}, "$.o"),
    ],
)
def test_canonical_bytes_refuses_values_outside_the_subset(value: object, path: str) -> None:
    from aksha.runtime.canonical import NotCanonical, canonical_bytes

    with pytest.raises(NotCanonical) as e:
        canonical_bytes(value)
    assert e.value.code == "not_canonical"
    assert e.value.path == path


def test_sha256_ref_is_prefixed_lowercase_hex() -> None:
    from aksha.runtime.canonical import sha256_ref

    ref = sha256_ref(b"abc")
    assert ref == "sha256:" + hashlib.sha256(b"abc").hexdigest()
    assert len(ref) == len("sha256:") + 64
