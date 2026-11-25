"""Systems of record for the outbox tests: a ledgered mock service and one with no ledger.

``Ledger`` keeps one row per idempotency key, as the mock send and form services do: a
known key returns its first receipt and changes nothing, and ``lookup`` answers by key.
Fault switches make it crash after the row lands (``crash_after_apply``) or fail
transiently a number of times (``transient``). ``NoLedger`` is a system that cannot be
asked, such as a webhook: every ``apply`` acts, and ``lookup`` is never trusted.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aksha.runtime.canonical import freeze
from aksha.runtime.outbox.errors import TransientEffectError
from aksha.runtime.outbox.types import SystemReceipt
from tests.support.steps import Crash


class Ledger:
    """A mock system of record that indexes the idempotency key."""

    def __init__(self, name: str = "mailer") -> None:
        self.name = name
        self.rows: dict[str, Mapping[str, Any]] = {}
        self.applied: list[str] = []  # every apply call, including the ones a key answered
        self.lookups: list[str] = []
        self.crash_after_apply = False
        self.crash_before_apply = False
        self.transient = 0
        self.transient_code = "transient"

    def ledger_id(self) -> str:
        return f"{self.name}-ledger-1"

    def apply(self, key: str, request: Mapping[str, Any]) -> SystemReceipt:
        self.applied.append(key)
        if self.crash_before_apply:
            raise Crash("before apply")
        if self.transient > 0:
            self.transient -= 1
            raise TransientEffectError(self.transient_code, "the service is busy")
        if key not in self.rows:
            self.rows[key] = freeze({"message_id": f"m-{len(self.rows) + 1}", **request})
        if self.crash_after_apply:
            raise Crash("after apply, before the outcome is recorded")
        return self._receipt(key)

    def lookup(self, key: str, request: Mapping[str, Any]) -> SystemReceipt | None:
        self.lookups.append(key)
        return self._receipt(key) if key in self.rows else None

    def _receipt(self, key: str) -> SystemReceipt:
        return SystemReceipt(key, self.ledger_id(), {"message_id": self.rows[key]["message_id"]})


class NoLedger:
    """A system with no ledger: every call acts, and nothing can say whether one landed."""

    def __init__(self, name: str = "hook") -> None:
        self.name = name
        self.fired: list[str] = []
        self.crash_after_apply = False
        self.fail = False

    def ledger_id(self) -> str:
        return f"{self.name}-none"

    def apply(self, key: str, request: Mapping[str, Any]) -> SystemReceipt:
        self.fired.append(key)
        if self.crash_after_apply:
            raise Crash("after the call")
        if self.fail:
            raise TransientEffectError(message="connection reset")
        return SystemReceipt(key, self.ledger_id(), {"status": 202})

    def lookup(self, key: str, request: Mapping[str, Any]) -> SystemReceipt | None:
        raise AssertionError("a system with no ledger is never asked")
