"""A live recording replays offline to the same outputs, under the settings it ran under.

The research agent runs ``AKSHA_MODE=live`` with every fast-class alias bound to the
LiteLLM adapters over a stubbed LiteLLM module (no network), under live settings that
are none of the defaults, and one classification call inside a tool body times out after
the calls before it were answered, so the tool step is retried; the retry asks only the
request that failed, the run's earlier answers being remembered (``FastAnswers``). The
replay runs with no ``AKSHA_LIVE_*`` at all: it must take every setting that shapes the
run from the recording, fail the recorded call the way it failed, and
end with the same outputs, the same limits on ``run.started`` and the same retry.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from aksha import data
from aksha.agents.research.fakes import ADAPTERS
from aksha.app import DEFAULT_AGENTS, DEFAULT_EXTENSIONS
from aksha.interfaces import wire
from aksha.interfaces.cli.main import main
from tests.support.live_stub import FakesLiteLLM
from tests.support.session import env

FAST = ("doc_kind", "page_facts", "price_table", "report_facts", "conversation_facts",
        "feedback_items")  # fmt: skip
REQUEST = (
    "Who sells camera-free occupancy sensing for offices in the EU, and at what price? "
    "Compare power (PoE, battery powered, mains) and privacy (privacy, camera, image)."
)
LIVE = {
    "AKSHA_MODE": "live",
    "AKSHA_ALIAS_GENERATE": "tests.support.live_stub:OfflineGeneration",
    **{f"AKSHA_LIVE_MODEL_{a.upper()}": f"stub:{a}" for a in FAST},
    # None of these is a default: each shapes the run, so the replay must take it from
    # the recording.
    "AKSHA_LIVE_TURN_FACTOR": "2",
    "AKSHA_LIVE_ACTIVE_MS": "1234000",
    "AKSHA_LIVE_TIMEOUT_S": "90",
    "AKSHA_LIVE_RETRIES": "4",
    "AKSHA_LIVE_BACKOFF_MS": "1500",
    "AKSHA_LIVE_BACKOFF_CAP_MS": "9000",
    "AKSHA_LIVE_REASONING_EFFORT": "low",
}


def _agents(tmp: Path) -> Path:
    root = tmp / "live-agents"
    if not (root / "research").is_dir():
        shutil.copytree(DEFAULT_AGENTS / "research", root / "research",
                        ignore=shutil.ignore_patterns("__pycache__"))  # fmt: skip
    return root


def aksha(tmp: Path, *argv: str, **extra: str) -> tuple[int, str]:
    out = io.StringIO()
    more = {"AKSHA_AGENTS": str(_agents(tmp)), "AKSHA_EXTENSIONS": str(DEFAULT_EXTENSIONS)}
    code = main(["--user", "o1", *argv], out=out, env=env(tmp, **more, **extra))
    return code, out.getvalue()


def _events(store: Path, *types: str) -> list[tuple[str, str | None, dict[str, Any]]]:
    (path,) = store.glob("*.sqlite")
    marks = ",".join("?" * len(types))
    with closing(sqlite3.connect(path)) as db:
        rows = db.execute(f"SELECT type, step, payload FROM events WHERE type IN ({marks})"
                          " ORDER BY seq", types).fetchall()  # fmt: skip
    return [(t, s, json.loads(p)) for t, s, p in rows]


def test_a_live_recording_replays_with_a_clean_environment_to_the_same_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib = FakesLiteLLM({a: ADAPTERS[a] for a in FAST}, fail=frozenset({20}))
    monkeypatch.setitem(sys.modules, "litellm", lib)
    rec = tmp_path / "rec"
    code, out = aksha(tmp_path, "run", REQUEST, "--agent", "research", "--input",
                      str(data.worlds() / "stillroom" / "sources"), "--record", str(rec),
                      AKSHA_SEARCH="world:stillroom", **LIVE)  # fmt: skip
    assert code in (wire.EXIT_OK, wire.EXIT_PARTIAL), out
    assert lib.calls and all(c["reasoning_effort"] == "low" and c["timeout"] == 90.0
                             for c in lib.calls)  # fmt: skip
    # The twentieth call timed out inside a tool body; its step failed once and ran again.
    failed = _events(rec / "store", "step.failed")
    assert [p["code"] for _, _, p in failed] == ["transient"], failed
    # The retry asked the failed request again and nothing the failed attempt was answered.
    asked = [json.dumps(c["messages"], sort_keys=True) for c in lib.calls]
    assert asked.count(asked[19]) == 2 and len(set(asked)) == len(asked) - 1
    lines = [json.loads(x) for x in (rec / "models.jsonl").read_text().splitlines()]
    assert [x["error"]["kind"] for x in lines if "error" in x] == ["transient"]
    manifest = json.loads((rec / "recording.json").read_text())
    assert manifest["turn_factors"] == {"research": 2}
    assert manifest["limits"]["research"] == {
        "active_ms": 1_234_000, "model_timeout_ms": 105_000, "max_retries": 4,
        "backoff": {"base_ms": 1500, "factor": 2, "cap_ms": 9000},
    }  # fmt: skip
    # The replay: no live settings, no LiteLLM, no world, no network.
    monkeypatch.setitem(sys.modules, "litellm", None)
    again = tmp_path / "again"
    code, out = aksha(tmp_path, "replay", str(rec), "--out", str(again))
    assert code in (wire.EXIT_OK, wire.EXIT_PARTIAL), out
    assert "outputs: the same as recorded" in out, out
    assert (again / "outputs.json").read_bytes() == (rec / "outputs.json").read_bytes()
    ((_, _, recorded),) = _events(rec / "store", "run.started")
    ((_, _, replayed),) = _events(again / "store", "run.started")
    for key in ("budget", "turn_factor"):
        assert replayed[key] == recorded[key], key

    def values(store: Path) -> list[tuple[str | None, Any]]:
        return [(s, p.get("value")) for _, s, p in _events(store, "step.completed")
                if p.get("kind") == "tool"]  # fmt: skip

    def calls_hashes(store: Path) -> list[Any]:
        return [[c["calls_hash"] for c in (v or {}).get("model_calls", [])]
                for _, v in values(store)]  # fmt: skip

    # Each tool step's model calls were answered as recorded, the failed attempt's too ...
    assert calls_hashes(again / "store") == calls_hashes(rec / "store")
    # ... and the same step failed the same way before it completed.
    assert [(s, p["code"]) for _, s, p in _events(again / "store", "step.failed")] == [
        (s, p["code"]) for _, s, p in failed
    ]
