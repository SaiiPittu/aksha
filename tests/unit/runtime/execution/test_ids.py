from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.storage.sqlite import open_store
from aksha.runtime.events.envelope import NewEvent
from aksha.runtime.execution.ids import LogIdSource, highest
from aksha.runtime.storage.ports import EventStore
from tests.support.events import START, probe_catalogue, run_started


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    s = open_store(
        tmp_path / "a.sqlite", FakeClock(START), writer=True, catalogue=probe_catalogue()
    )
    yield s
    s.close()


def test_ids_count_from_the_store_per_owner_and_prefix(store: EventStore) -> None:
    ids = LogIdSource(store)
    assert ids.next("o", "R") == "R-0001"
    assert ids.next("o", "R") == "R-0002"  # issued, not yet written
    assert ids.next("p", "R") == "R-0001"  # per owner
    assert ids.next("o", "A") == "A-0001"  # per prefix


def test_a_restart_continues_the_sequence_from_the_store(store: EventStore) -> None:
    store.append("o", "U1", (run_started("R-0007"),))
    assert LogIdSource(store).next("o", "R") == "R-0008"


def test_ids_in_payload_strings_count(store: EventStore) -> None:
    store.append("o", "U2", (NewEvent("probe.noted", {"n": 1, "text": "A-0004"}, run="R-0001"),))
    assert LogIdSource(store).next("o", "A") == "A-0005"


def test_highest_ignores_other_shapes(store: EventStore) -> None:
    store.append("o", "U2", (NewEvent("probe.noted", {"n": 1, "text": "R-12x"}, run="R-0003"),))
    assert highest(store.read("o"), "R") == 3


@pytest.mark.parametrize("prefix", ["", "r", "R-", "TOOLONGPREFIX"])
def test_a_prefix_is_capital_letters(store: EventStore, prefix: str) -> None:
    with pytest.raises(ValueError, match="prefix"):
        LogIdSource(store).next("o", prefix)
