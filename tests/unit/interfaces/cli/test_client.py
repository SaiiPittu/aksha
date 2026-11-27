"""The CLI's server client sends ``AKSHA_TOKEN`` as a bearer token, and the owner header."""

from __future__ import annotations

import io
import urllib.request
from typing import Any

import pytest

from aksha.app import RuntimeConfig
from aksha.interfaces.cli import main as cli_main
from aksha.interfaces.cli.client import Client


class Response(io.BytesIO):
    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[urllib.request.Request]:
    seen: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> Response:
        seen.append(request)
        return Response(b'{"runs": []}')

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return seen


def test_a_token_goes_as_a_bearer_header(sent: list[urllib.request.Request]) -> None:
    Client("http://h:1", "o1", token="aksha_pat_abc").get("/runs")
    (request,) = sent
    assert request.get_header("Authorization") == "Bearer aksha_pat_abc"
    assert request.get_header("X-aksha-user") == "o1"


def test_without_a_token_only_the_owner_header_goes(sent: list[urllib.request.Request]) -> None:
    Client("http://h:1", "o1").get("/runs")
    (request,) = sent
    assert request.get_header("Authorization") is None
    assert request.get_header("X-aksha-user") == "o1"


def test_the_cli_reads_the_token_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    made: list[Client] = []

    def handler(ctx: cli_main.Context, _: object) -> int:
        found = ctx.server()
        assert found is not None
        made.append(found)
        return 0

    monkeypatch.setattr(cli_main, "find_server", lambda url, path: "http://h:1")
    parser = cli_main.build_parser()
    real_parse = parser.parse_args

    def parse(args: list[str]) -> Any:
        ns = real_parse(args)
        ns.handler = handler
        return ns

    monkeypatch.setattr(parser, "parse_args", parse)
    monkeypatch.setattr(cli_main, "build_parser", lambda: parser)
    env = {"AKSHA_HOME": str(tmp_path), "AKSHA_TOKEN": "aksha_pat_xyz"}
    assert cli_main.main(["--user", "o1", "status"], out=io.StringIO(), env=env) == 0
    assert made[0].token == "aksha_pat_xyz" and made[0].owner == "o1"
    assert RuntimeConfig.from_env(env).server_url is None
