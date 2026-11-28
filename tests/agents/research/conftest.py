"""Close every research bench a test opened, so no SQLite connection outlives its test.

A bench a wider-scoped fixture shares (the session's episodes) was opened before the test
started and is left open for the tests that share it.
"""

from __future__ import annotations

import gc
from collections.abc import Iterator

import pytest

from tests.support.research import close_benches_opened_since, open_benches


@pytest.fixture(autouse=True)
def _close_benches() -> Iterator[None]:
    before = open_benches()
    yield
    close_benches_opened_since(before)
    gc.collect()  # a connection this test leaked is reported here, in this test
