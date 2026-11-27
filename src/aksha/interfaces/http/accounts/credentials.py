"""Passwords (argon2id), random secrets and their digests, and the shape checks on sign-up.

A password is kept only as its argon2id hash (``argon2-cffi``, part of the ``server``
extra). Session cookies, API tokens and invite links are random secrets that the server
shows once; the accounts database keeps only their SHA-256 digests, so a copy of it
signs nobody in.
"""

from __future__ import annotations

import hashlib
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from aksha.interfaces.http.accounts.errors import AccountError

TOKEN_PREFIX = "aksha_pat_"
"""What every API token starts with, so a leaked one is easy to recognise and scan for."""

MIN_PASSWORD = 8
MAX_PASSWORD = 1024
"""Longer passwords are refused: hashing an unbounded input is a denial of service."""

MAX_EMAIL = 254
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Passwords:
    """Hash and verify passwords with argon2id; the defaults are the library's own."""

    def __init__(self, *, time_cost: int = 3, memory_cost: int = 65536,
                 parallelism: int = 4) -> None:  # fmt: skip
        """Hold a hasher; tests pass cheaper parameters, a server keeps the defaults."""
        self._hasher = PasswordHasher(
            time_cost=time_cost, memory_cost=memory_cost, parallelism=parallelism
        )
        self._decoy = self._hasher.hash(secrets.token_hex(16))

    def hash(self, password: str) -> str:
        """The argon2id hash of ``password``, salted, in the PHC string format."""
        return self._hasher.hash(password)

    def verify(self, stored: str | None, password: str) -> bool:
        """Whether ``password`` matches ``stored``.

        With no stored hash (an unknown email, a guest), a decoy hash is checked instead,
        so the answer takes as long as for a real account and reveals nothing.
        """
        try:
            return self._hasher.verify(stored or self._decoy, password) and stored is not None
        except (VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, stored: str) -> bool:
        """Whether ``stored`` was hashed with other parameters than these."""
        return self._hasher.check_needs_rehash(stored)


def new_secret(prefix: str = "") -> str:
    """A random secret of 256 bits, URL-safe, after ``prefix``."""
    return prefix + secrets.token_urlsafe(32)


def new_id(prefix: str) -> str:
    """A random identifier such as ``u-3f9c...``; never derived from what it names."""
    return f"{prefix}-{secrets.token_hex(8)}"


def digest(secret: str) -> str:
    """The SHA-256 hex digest under which a secret is stored and looked up."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def check_password(password: object) -> str:
    """``password`` when it is a string of 8 to 1024 characters; else ``weak_password``."""
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise AccountError(
            422, "weak_password", f"a password has {MIN_PASSWORD} to {MAX_PASSWORD} characters"
        )
    return password


def check_email(email: object) -> str:
    """``email`` trimmed, when it looks like an address; else ``validation``."""
    if not isinstance(email, str):
        raise AccountError(422, "validation", "email is a string")
    trimmed = email.strip()
    if len(trimmed) > MAX_EMAIL or not _EMAIL.match(trimmed):
        raise AccountError(422, "validation", "email is not an email address")
    return trimmed
