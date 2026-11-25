from types import MappingProxyType

from aksha.runtime.storage.rows import StoredArtifact
from tests.support.events import artifact_row


def test_artifact_row_freezes_its_content() -> None:
    row = artifact_row()
    assert isinstance(row.content, MappingProxyType)
    assert row.content == {"body": "hello"}


def test_stored_artifact_names_its_owner_and_event() -> None:
    stored = StoredArtifact(owner="o1", event_seq=2, row=artifact_row())
    assert stored.ref == "note/A@v1"
