"""The CI shards (``--shard K/N``, tests/conftest.py): pinned files exist, the deal is fixed.

The CI job "Test shards cover the suite" proves on the real collection that the shards
never overlap and add up to the whole suite; these check the pieces that job cannot see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import SHARD_PINNED_FILES, _parse_shard, shard_of

ROOT = Path(__file__).resolve().parent.parent


def test_every_pinned_file_exists_so_a_rename_cannot_silently_unpin_it() -> None:
    missing = [path for path in SHARD_PINNED_FILES if not (ROOT / path).is_file()]
    assert missing == []
    assert len(set(SHARD_PINNED_FILES)) == len(SHARD_PINNED_FILES)


def test_pinned_files_are_dealt_out_and_back_across_the_shards() -> None:
    dealt = [shard_of(f"{path}::test_x", "any key", 4) for path in SHARD_PINNED_FILES]
    assert dealt == [1, 2, 3, 4, 4, 3, 2, 1, 1, 2, 3, 4]


@pytest.mark.parametrize("count", [1, 2, 4, 7])
def test_a_hashed_test_gets_one_shard_in_range_and_the_same_one_every_time(count: int) -> None:
    keys = [f"tests/unit/test_{n}.py::test_{n}" for n in range(200)]
    first = [shard_of(key, key, count) for key in keys]
    assert all(1 <= shard <= count for shard in first)
    assert first == [shard_of(key, key, count) for key in keys]
    if count > 1:
        assert len(set(first)) == count  # 200 keys reach every shard


@pytest.mark.parametrize("value", ["", "4", "0/4", "5/4", "a/4", "1/0", "1/4/2"])
def test_a_bad_shard_is_a_usage_error(value: str) -> None:
    with pytest.raises(pytest.UsageError):
        _parse_shard(value)


def test_a_good_shard_parses() -> None:
    assert _parse_shard("3/4") == (3, 4)
