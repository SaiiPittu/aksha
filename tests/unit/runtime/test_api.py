import aksha.runtime.api as api
from aksha.runtime.definitions.versions import parse_version


def test_api_exports_resolve() -> None:
    assert all(hasattr(api, name) for name in api.__all__)
    assert len(set(api.__all__)) == len(api.__all__)


def test_api_keeps_the_event_store_names() -> None:
    for name in ("ArtifactsView", "Event", "EventTypeSpec", "LogView", "NewEvent", "Reducer"):
        assert name in api.__all__


def test_api_version_is_major_minor() -> None:
    assert parse_version(api.API_VERSION) == (1, 2)


def test_api_exports_what_a_save_names() -> None:
    for name in ("ArtifactRef", "InputRef", "RelationRequest", "Excerpt", "SELF", "SaveRequest"):
        assert name in api.__all__
