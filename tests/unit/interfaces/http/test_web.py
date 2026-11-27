"""The built web app beside the API: files as built, ``index.html`` for the app's routes."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aksha.interfaces.http.serve import web_dist
from aksha.interfaces.http.web import LONG_CACHE, NO_CACHE, WebDistMissing, mount_web


def _dist(tmp: Path) -> Path:
    dist = tmp / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp / "secret.txt").write_text("outside", encoding="utf-8")
    return dist


def _client(tmp: Path) -> TestClient:
    app = FastAPI()

    @app.get("/api/v1/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    mount_web(app, _dist(tmp))
    return TestClient(app)


def test_hashed_assets_are_cached_long_and_other_files_are_not(tmp_path: Path) -> None:
    client = _client(tmp_path)
    js = client.get("/assets/index-abc123.js")
    assert js.status_code == 200 and js.text == "console.log(1)"
    assert js.headers["cache-control"] == LONG_CACHE
    icon = client.get("/favicon.svg")
    assert icon.status_code == 200 and icon.headers["cache-control"] == NO_CACHE


@pytest.mark.parametrize("path", ["/", "/s/S-0123456789ab", "/settings/members", "/index.html"])
def test_app_routes_get_index_html_uncached(tmp_path: Path, path: str) -> None:
    res = _client(tmp_path).get(path)
    assert res.status_code == 200 and "id=root" in res.text
    assert res.headers["content-type"].startswith("text/html")
    assert res.headers["cache-control"] == NO_CACHE


def test_api_routes_win_and_unknown_api_paths_stay_json_404(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/api/v1/health").json() == {"ok": True}
    res = client.get("/api/v1/nothing-here")
    assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"


def test_a_path_outside_the_build_is_never_sent(tmp_path: Path) -> None:
    res = _client(tmp_path).get("/assets/%2e%2e/%2e%2e/secret.txt")
    assert "outside" not in res.text


def test_a_folder_without_index_html_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WebDistMissing):
        mount_web(FastAPI(), tmp_path)


def test_the_flag_wins_over_the_environment_and_none_serves_nothing(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    assert web_dist(None, {}) is None
    assert web_dist(None, {"AKSHA_WEB_DIST": str(dist)}) == dist.resolve()
    assert web_dist(dist, {"AKSHA_WEB_DIST": str(tmp_path / "nope")}) == dist.resolve()
    with pytest.raises(WebDistMissing):
        web_dist(None, {"AKSHA_WEB_DIST": str(tmp_path / "nope")})


@pytest.mark.parametrize("path", ["/", "/s/S-1", "/assets/index-abc123.js", "/favicon.svg"])
def test_every_web_response_refuses_to_be_framed(tmp_path: Path, path: str) -> None:
    res = _client(tmp_path).get(path)
    assert res.status_code == 200
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert res.headers["x-content-type-options"] == "nosniff"
