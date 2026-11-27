"""``AKSHA_MODE=live`` end to end, against a chat-completions server on localhost (ADR-P07 G2, G3).

The research agent runs from the command line on the third fixture world with every
fast-class alias bound to the built-in live adapters, which call
:class:`~tests.support.live_stub.StubServer` over HTTP: the decide step, each kind's schema
and the column and theme classifiers go over the wire as a general model in adapted mode
would answer them, and each quote comes back verbatim and is located in code. Generation is
the agent's rule-based policy, named as a live adapter (``AKSHA_ALIAS_GENERATE``), because a
real policy needs a real model. No test reaches the network.
"""

from __future__ import annotations

import io
import json
import re
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from aksha import data
from aksha.agents.research.fakes import ADAPTERS
from aksha.app import DEFAULT_AGENTS, DEFAULT_EXTENSIONS
from aksha.interfaces import wire
from aksha.interfaces.cli.main import main
from tests.support.live_stub import StubServer
from tests.support.session import env

CINDRAL = data.world_path("cindral") / "sources"
CASE = (data.suites() / "cindral" / "h-research-happy.toml").read_text("utf-8")
PROMPT = re.search(r'^opening = "(.*)"$', CASE, re.M).group(1)  # type: ignore[union-attr]
KEY = "sk-local-test-only"
FAST = ("doc_kind", "page_facts", "price_table", "report_facts", "conversation_facts",
        "feedback_items")


@pytest.fixture
def stub() -> Iterator[StubServer]:
    server = StubServer({a: ADAPTERS[a] for a in FAST})
    yield server
    server.close()


def _agents(tmp: Path) -> Path:
    """A folder of agent folders holding only the research agent."""
    root = tmp / "live-agents"
    if (root / "research").is_dir():
        return root
    shutil.copytree(DEFAULT_AGENTS / "research", root / "research",
                    ignore=shutil.ignore_patterns("__pycache__"))  # fmt: skip
    return root


def aksha(tmp: Path, live: dict[str, str], *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    extra = {"AKSHA_AGENTS": str(_agents(tmp)), "AKSHA_EXTENSIONS": str(DEFAULT_EXTENSIONS),
             "AKSHA_MODE": "live", **live}  # fmt: skip
    code = main(["--user", "o1", *argv], out=out, env=env(tmp, **extra))
    return code, out.getvalue()


def _live(stub: StubServer, **more: str) -> dict[str, str]:
    out = {
        "AKSHA_LIVE_BASE_URL": stub.base_url,
        "AKSHA_LIVE_API_KEY": KEY,
        "AKSHA_ALIAS_GENERATE": "tests.support.live_stub:OfflineGeneration",
        **{f"AKSHA_LIVE_MODEL_{a.upper()}": f"stub:{a}" for a in FAST},
    }
    out.update(more)
    return out


def _started(tmp: Path) -> dict[str, object]:
    from aksha.adapters.clock.fake import FakeClock
    from aksha.adapters.storage.sqlite import open_store
    from aksha.runtime.canonical import thaw
    from tests.support.events import START

    store = open_store(tmp / "var" / "aksha.sqlite", FakeClock(START), writer=False)
    try:
        (e,) = [e for e in store.read("o1") if e.type == "run.started"]
        started: dict[str, object] = thaw(e.payload)
        return started
    finally:
        store.close()


def test_the_research_agent_runs_live_over_http_with_every_fast_alias_on_the_wire(
    tmp_path: Path, stub: StubServer
) -> None:
    code, out = aksha(tmp_path, _live(stub), "run", PROMPT, "--agent", "research",
                      "--input", str(CINDRAL))  # fmt: skip
    assert code == wire.EXIT_OK, out
    assert "R-0001 research ended complete" in out, out
    # The decide step and the schemas were asked over HTTP, each by the
    # model its own setting names, and the key travelled only in the Authorization header.
    asked = {r["body"]["model"] for r in stub.requests}
    assert {"stub:doc_kind", "stub:page_facts", "stub:price_table"} <= asked, asked
    for r in stub.requests:
        assert r["path"] == "/v1/chat/completions"
        assert r["headers"]["Authorization"] == f"Bearer {KEY}"
        assert KEY not in json.dumps(r["body"])
    # The run records each binding's adapter class and that the fast ones are adapted.
    started = _started(tmp_path)
    assert started["run_mode"] == "live"
    binding = {b["alias"]: b for b in started["models"]}  # type: ignore[attr-defined]
    assert binding["page_facts"]["adapter"].endswith("LiveExtractionModel")
    assert binding["doc_kind"]["adapter"].endswith("LiveClassificationModel")
    assert binding["page_facts"]["native"] is False and binding["generate"]["native"] is True
    assert KEY not in json.dumps(_started(tmp_path))
    # What the live readers found is what the offline run finds: the same counts.
    code, shown = aksha(tmp_path, _live(stub), "show", "research.needs")
    assert code == wire.EXIT_OK and '"phrase": "6 of 16 reviews in this sample"' in shown, shown
    code, shown = aksha(tmp_path, _live(stub), "show", "research.competitors")
    assert code == wire.EXIT_OK and '"resolved": 4' in shown, shown


def test_a_remote_server_without_a_key_is_refused_before_any_call(
    tmp_path: Path, stub: StubServer
) -> None:
    live = _live(stub, AKSHA_LIVE_BASE_URL="https://models.example.invalid/v1")
    del live["AKSHA_LIVE_API_KEY"]
    code, out = aksha(tmp_path, live, "run", PROMPT, "--agent", "research",
                      "--input", str(CINDRAL))  # fmt: skip
    assert code != wire.EXIT_OK and "live_key_missing" in out, out
    assert "AKSHA_LIVE_API_KEY" in out and stub.requests == []


def test_a_missing_server_or_model_names_the_setting_to_set(
    tmp_path: Path, stub: StubServer
) -> None:
    live = _live(stub)
    del live["AKSHA_LIVE_BASE_URL"]
    del live["AKSHA_LIVE_MODEL_PAGE_FACTS"]
    code, out = aksha(tmp_path, live, "run", PROMPT, "--agent", "research",
                      "--input", str(CINDRAL))  # fmt: skip
    assert code != wire.EXIT_OK and "live_config_missing" in out, out
    assert "AKSHA_LIVE_BASE_URL" in out and "AKSHA_LIVE_MODEL_PAGE_FACTS" in out
    assert stub.requests == []
