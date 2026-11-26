"""Fixtures shared by the integration tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.support.tasks import StoreLedger


@pytest.fixture(autouse=True)
def _close_store_ledgers() -> Iterator[None]:
    """Close every ``StoreLedger`` a test opened, so no SQLite connection leaks into the next."""
    yield
    StoreLedger.close_all()
