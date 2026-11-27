"""``aksha serve`` reads its auth settings: real accounts by default, the header on request."""

from __future__ import annotations

from pathlib import Path

import pytest

from aksha.app import RuntimeConfig, StorageConfig
from aksha.interfaces.http.common import MB, allowed_hosts, upload_max_bytes
from aksha.interfaces.http.serve import auth_config, host_warning, server_url


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


# --- the Host allow-list (DNS rebinding) ----------------------------------------------------


@pytest.mark.parametrize(
    "host",
    ["localhost", "localhost:8765", "LOCALHOST:8765", "app.localhost:5173", "127.0.0.1:8765",
     "127.0.0.2", "[::1]:8765", "::1", "localhost."],
)  # fmt: skip
def test_loopback_names_are_always_allowed(host: str) -> None:
    assert allowed_hosts("0.0.0.0", {}).allows(host)


@pytest.mark.parametrize(
    "host",
    ["attacker.example", "attacker.example:8765", "localhost.attacker.example", "10.0.0.5:8765",
     "", None, "[::1", "aksha.example:notaport"],
)  # fmt: skip
def test_any_other_host_is_refused_unless_named(host: str | None) -> None:
    assert not allowed_hosts("0.0.0.0", {}).allows(host)


def test_the_bind_address_and_the_environment_add_names() -> None:
    hosts = allowed_hosts("10.0.0.5", {"AKSHA_ALLOWED_HOSTS": " Aksha.Example , ops.example:8443,"})
    for host in ("10.0.0.5:8765", "aksha.example", "aksha.example:443", "ops.example:8765"):
        assert hosts.allows(host), host
    assert not hosts.allows("attacker.example")
    for wildcard in ("0.0.0.0", "::", ""):
        assert allowed_hosts(wildcard, {}).names == frozenset()


def test_a_wildcard_bind_without_named_hosts_warns_how_to_add_one() -> None:
    warning = host_warning("0.0.0.0", {})
    assert warning is not None
    assert "only loopback names" in warning and "AKSHA_ALLOWED_HOSTS" in warning
    assert host_warning("::", {}) is not None
    assert host_warning("0.0.0.0", {"AKSHA_ALLOWED_HOSTS": "aksha.example"}) is None
    assert host_warning("127.0.0.1", {}) is None


def test_the_server_file_names_a_url_the_allow_list_accepts() -> None:
    assert server_url("0.0.0.0", 8765) == "http://127.0.0.1:8765"
    assert server_url("::", 8765) == "http://[::1]:8765"
    assert server_url("::1", 8765) == "http://[::1]:8765"
    assert server_url("aksha.example", 80) == "http://aksha.example:80"
    for bind in ("0.0.0.0", "::", "127.0.0.1", "10.0.0.5"):
        netloc = server_url(bind, 8765).removeprefix("http://")
        assert allowed_hosts(bind, {}).allows(netloc), bind
