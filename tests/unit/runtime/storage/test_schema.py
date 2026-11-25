import pytest

from aksha.runtime.storage.errors import StoreError
from aksha.runtime.storage.schema import SCHEMA_VERSION, check_schema_version


def test_check_schema_version_accepts_only_this_version() -> None:
    check_schema_version(SCHEMA_VERSION)
    for found in (None, 0, SCHEMA_VERSION + 1):
        with pytest.raises(StoreError) as e:
            check_schema_version(found)
        assert e.value.code == "schema_mismatch"
