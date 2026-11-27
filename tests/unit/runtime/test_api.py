import aksha.runtime.api as api
from aksha.runtime.definitions.versions import parse_version


def test_api_exports_resolve() -> None:
    assert all(hasattr(api, name) for name in api.__all__)
    assert len(set(api.__all__)) == len(api.__all__)


def test_api_keeps_the_event_store_names() -> None:
    for name in ("ArtifactsView", "Event", "EventTypeSpec", "LogView", "NewEvent", "Reducer"):
        assert name in api.__all__


def test_api_version_is_major_minor() -> None:
    assert parse_version(api.API_VERSION) == (2, 13)


def test_api_exports_what_a_save_names() -> None:
    for name in ("ArtifactRef", "InputRef", "RelationRequest", "Excerpt", "SELF", "SaveRequest"):
        assert name in api.__all__


def test_api_exports_what_grounding_asks_of_an_agent() -> None:
    for name in ("SpanDraft", "CitePolicy", "Unknown", "maybe_unknown"):
        assert name in api.__all__


def test_api_exports_what_a_contributed_system_of_record_implements() -> None:
    for name in ("SystemOfRecordSpec", "SystemOfRecord", "SystemReceipt"):
        assert name in api.__all__


def test_api_exports_what_an_extension_system_raises_and_what_external_state_reads() -> None:
    from aksha.runtime.outbox.errors import TransientEffectError
    from aksha.runtime.outbox.records import ExternalRecord, external_records

    assert api.TransientEffectError is TransientEffectError
    assert (api.ExternalRecord, api.external_records) == (ExternalRecord, external_records)
