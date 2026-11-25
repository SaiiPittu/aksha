import pytest

from aksha.runtime.outbox.errors import KeyFieldMissing
from aksha.runtime.outbox.keys import idempotency_key, key_values, request_hash
from aksha.runtime.outbox.types import EffectSpec

SEND = EffectSpec(system="mailer", key_fields=("/document", "/to/id"), semantics="reconcile")
ARGS = {"document": "packet/P1@v2", "to": {"id": "r-7", "name": "A"}, "note": "hi"}


def test_key_is_built_from_the_key_fields_only() -> None:
    key = idempotency_key("send", "o", ARGS, SEND)
    assert key.startswith("sha256:") and len(key) == 7 + 64
    reworded = {**ARGS, "note": "a different cover note", "to": {"id": "r-7", "name": "B"}}
    assert idempotency_key("send", "o", reworded, SEND) == key


def test_key_changes_with_what_the_effect_means() -> None:
    key = idempotency_key("send", "o", ARGS, SEND)
    assert idempotency_key("send", "o", {**ARGS, "document": "packet/P1@v3"}, SEND) != key
    assert idempotency_key("send", "o", {**ARGS, "to": {"id": "r-8"}}, SEND) != key
    assert idempotency_key("post", "o", ARGS, SEND) != key


def test_effect_key_includes_owner() -> None:
    assert idempotency_key("send", "o", ARGS, SEND) != idempotency_key("send", "p", ARGS, SEND)


def test_key_never_depends_on_where_in_a_run_it_happened() -> None:
    # The arguments are all there is: no step ID, run or clock enters the key (D2).
    assert idempotency_key("send", "o", dict(ARGS), SEND) == idempotency_key(
        "send", "o", dict(reversed(list(ARGS.items()))), SEND
    )


def test_resolve_pins_each_reference_to_its_exact_version() -> None:
    latest = {"packet/P1": "packet/P1@v2"}

    def resolve(pointer: str, value: object) -> object:
        return latest.get(str(value), value) if pointer == "/document" else value

    floating = {**ARGS, "document": "packet/P1"}
    assert idempotency_key("send", "o", floating, SEND, resolve=resolve) == idempotency_key(
        "send", "o", ARGS, SEND
    )
    latest["packet/P1"] = "packet/P1@v3"
    assert idempotency_key("send", "o", floating, SEND, resolve=resolve) != idempotency_key(
        "send", "o", ARGS, SEND
    )


def test_missing_key_field_is_refused() -> None:
    with pytest.raises(KeyFieldMissing) as e:
        key_values({"document": "packet/P1@v2"}, SEND)
    assert e.value.code == "key_field_missing"
    assert "/to/id" in e.value.message


def test_key_values_are_read_by_json_pointer() -> None:
    assert key_values(ARGS, SEND) == {"/document": "packet/P1@v2", "/to/id": "r-7"}


def test_request_hash_is_over_the_whole_request() -> None:
    assert request_hash(ARGS) != request_hash({**ARGS, "note": "other"})
    assert request_hash(ARGS).startswith("sha256:")
