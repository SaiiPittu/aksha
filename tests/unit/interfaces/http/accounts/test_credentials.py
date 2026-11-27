"""Passwords hash with argon2id; session and API tokens are random and stored as digests."""

from __future__ import annotations

import pytest

from aksha.interfaces.http.accounts.credentials import (
    TOKEN_PREFIX,
    Passwords,
    check_email,
    check_password,
    digest,
    new_secret,
)
from aksha.interfaces.http.accounts.service import AccountError

FAST = Passwords(time_cost=1, memory_cost=8, parallelism=1)


def test_a_password_hash_is_argon2id_and_verifies_only_the_password() -> None:
    stored = FAST.hash("correct horse battery")
    assert stored.startswith("$argon2id$")
    assert "correct horse" not in stored
    assert FAST.verify(stored, "correct horse battery")
    assert not FAST.verify(stored, "correct horse battery!")
    assert FAST.hash("correct horse battery") != stored  # salted


def test_verify_without_a_stored_hash_or_with_a_broken_one_is_false() -> None:
    assert not FAST.verify(None, "anything at all")
    assert not FAST.verify("not-a-hash", "anything at all")


def test_the_default_parameters_are_the_library_defaults() -> None:
    stored = Passwords().hash("a long enough password")
    assert stored.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert FAST.needs_rehash(FAST.hash("x" * 10)) is False
    assert Passwords().needs_rehash(FAST.hash("x" * 10)) is True


def test_secrets_are_random_and_digests_are_sha256_hex() -> None:
    a, b = new_secret(TOKEN_PREFIX), new_secret(TOKEN_PREFIX)
    assert a != b and a.startswith("aksha_pat_") and len(a) > 40
    assert digest(a) == digest(a) and digest(a) != digest(b)
    assert len(digest(a)) == 64 and a not in digest(a)


@pytest.mark.parametrize("password", ["short", "x" * 1025])
def test_a_password_outside_the_length_bounds_is_refused(password: str) -> None:
    with pytest.raises(AccountError) as raised:
        check_password(password)
    assert raised.value.code == "weak_password" and raised.value.status == 422


@pytest.mark.parametrize("email", ["", "no-at-sign", "a@b", "a b@c.example", "x@" + "y" * 260])
def test_a_malformed_email_is_refused(email: str) -> None:
    with pytest.raises(AccountError) as raised:
        check_email(email)
    assert raised.value.code == "validation"


def test_an_email_is_trimmed() -> None:
    assert check_email("  Ada@Example.org ") == "Ada@Example.org"
