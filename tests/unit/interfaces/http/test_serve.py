"""``aksha serve`` reads its auth settings: real accounts by default, the header on request."""

from __future__ import annotations

from pathlib import Path

import pytest

from aksha.app import RuntimeConfig, StorageConfig
from aksha.interfaces.http.common import MB, upload_max_bytes
from aksha.interfaces.http.serve import auth_config


def _config(tmp: Path) -> RuntimeConfig:
    return RuntimeConfig(storage=StorageConfig(sqlite_path=tmp / "var" / "aksha.sqlite",
                                               blob_path=tmp / "var" / "b"))  # fmt: skip


def test_the_default_is_real_accounts_beside_the_store(tmp_path: Path) -> None:
    auth = auth_config(_config(tmp_path), {}, trust_user_header=False)
    try:
        assert auth.accounts is not None and not auth.trust_user_header
        assert auth.accounts.store.path == tmp_path / "var" / "accounts.sqlite"
        assert auth.guests is False and auth.cookie_secure is False
    finally:
        auth.close()


def test_the_environment_moves_the_database_and_turns_on_guests(tmp_path: Path) -> None:
    env = {"AKSHA_ACCOUNTS_PATH": str(tmp_path / "elsewhere.sqlite"), "AKSHA_GUESTS": "1",
           "AKSHA_COOKIE_SECURE": "1"}  # fmt: skip
    auth = auth_config(_config(tmp_path), env, trust_user_header=False)
    try:
        assert auth.accounts is not None
        assert auth.accounts.store.path == tmp_path / "elsewhere.sqlite"
        assert auth.guests is True and auth.cookie_secure is True
    finally:
        auth.close()


def test_trusting_the_header_opens_no_accounts_database(tmp_path: Path) -> None:
    auth = auth_config(_config(tmp_path), {}, trust_user_header=True)
    assert auth.trust_user_header and auth.accounts is None
    assert not (tmp_path / "var" / "accounts.sqlite").exists()


def test_the_upload_cap_is_50_mb_unless_the_environment_names_another() -> None:
    assert upload_max_bytes({}) == 50 * MB
    assert upload_max_bytes({"AKSHA_UPLOAD_MAX_MB": "2"}) == 2 * MB
    assert upload_max_bytes({"AKSHA_UPLOAD_MAX_MB": "0.5"}) == MB // 2
    for bad in ("lots", "0", "-1"):
        with pytest.raises(ValueError, match="AKSHA_UPLOAD_MAX_MB"):
            upload_max_bytes({"AKSHA_UPLOAD_MAX_MB": bad})
