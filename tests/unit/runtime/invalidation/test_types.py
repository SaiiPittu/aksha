from aksha.runtime.invalidation.types import VersionedSourceSpec


def test_versioned_source_spec_names_its_root() -> None:
    spec = VersionedSourceSpec("notes", root="world/notes", collections=("notes",))
    assert (spec.name, spec.root, spec.max_age_s) == ("notes", "world/notes", None)
