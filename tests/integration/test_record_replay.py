"""Record a run, then replay it fully offline: the same outputs, a trace, nothing invented.

``aksha run --record`` runs one agent to its end in a store inside the recording folder,
appending every model call, search and fetch; ``aksha replay`` runs it again from the
recording alone (models answered from it, the web from it, the network off) and compares
the outputs. The bundled example recording is replayed the same way, and a recording
whose answers do not match the run it replays is refused, never filled in.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from aksha import data
from aksha.app import DEFAULT_AGENTS, DEFAULT_EXTENSIONS, RuntimeConfig, replay_run
from aksha.interfaces import wire
from aksha.interfaces.cli.main import main
from tests.support.session import env

EXAMPLE = data.recordings() / "research-occupancy"
LIVE = data.recordings() / "research-live-gpt"
REQUEST = (
    "Who sells camera-free occupancy sensing for offices in the EU, and at what price? "
    "Compare power (PoE, battery powered, mains) and privacy (privacy, camera, image)."
)


def aksha(tmp: Path, *argv: str, **extra: str) -> tuple[int, str]:
    out = io.StringIO()
    more = {"AKSHA_AGENTS": str(DEFAULT_AGENTS), "AKSHA_EXTENSIONS": str(DEFAULT_EXTENSIONS)}
    code = main(["--user", "o1", *argv], out=out, env=env(tmp, **more, **extra))
    return code, out.getvalue()


def _uploads(tmp: Path) -> Path:
    folder = tmp / "uploads" / "priorities"
    folder.mkdir(parents=True)
    note = data.worlds() / "stillroom" / "sources" / "priorities" / "business-priorities.md"
    shutil.copyfile(note, folder / note.name)
    return tmp / "uploads"


def test_a_recorded_run_replays_offline_to_the_same_outputs(tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    code, out = aksha(tmp_path, "run", REQUEST, "--agent", "research", "--input",
                      str(_uploads(tmp_path)), "--record", str(rec),
                      AKSHA_SEARCH="world:stillroom")  # fmt: skip
    assert code == wire.EXIT_OK, out
    assert "recorded R-0001 research (complete)" in out and "3 searches" in out, out
    manifest = json.loads((rec / "recording.json").read_text())
    assert manifest["script"]["agent"] == "research" and manifest["inputs"] == [
        "priorities/business-priorities.md"
    ]
    assert manifest["search"] == "world"
    assert "turn_factors" not in manifest  # an offline run keeps its own limits
    lines = (rec / "models.jsonl").read_text().splitlines()
    assert {json.loads(x)["class"] for x in lines} == {"generation", "extraction",
                                                        "classification"}  # fmt: skip
    fetched = [json.loads(x)["url"] for x in (rec / "fetch.jsonl").read_text().splitlines()]
    assert "https://kelvra.example/product" in fetched
    assert "https://sensor-deals.example/occupancy" not in fetched  # the advert was not read
    # The replay sees no world, no network and no fake model: only the recording.
    code, out = aksha(tmp_path, "replay", str(rec), "--out", str(tmp_path / "again"))
    assert code == wire.EXIT_OK, out
    assert "outputs: the same as recorded (11 outputs)" in out, out
    assert list((tmp_path / "again" / "trace").rglob("*.otlp.jsonl"))


def test_a_question_left_unanswered_ends_the_recording_waiting_on_it(tmp_path: Path) -> None:
    rec = str(tmp_path / "rec")
    code, out = aksha(tmp_path, "run", "Research the competitors.", "--agent", "research",
                      "--record", rec, AKSHA_SEARCH="world:stillroom")  # fmt: skip
    assert code == wire.EXIT_WAITING_INPUT, out
    assert "waiting on input segment" in out and "--answer" in out, out


def test_the_bundled_example_replays_to_its_recorded_outputs(tmp_path: Path) -> None:
    done = replay_run(RuntimeConfig(), EXAMPLE, tmp_path / "replay")
    assert done.same and done.run.outcome == "complete", done.differing
    again = (tmp_path / "replay" / "outputs.json").read_bytes()
    assert again == (EXAMPLE / "outputs.json").read_bytes()  # byte for byte
    assert done.run.trace
    competitors = done.run.outputs["research.competitors"]
    assert competitors["resolved"] == 5
    skipped = {s["why"] for s in competitors["web"]["skipped"]}
    assert skipped == {"an advert", "a copy of a page already chosen",
                       "an older version of a page on the same site"}  # fmt: skip


@pytest.mark.xfail(
    strict=True,
    reason=(
        "stale recording: since each keyed instance's context names its key and the "
        "evidence Task gives a refused finish back to its model, the recorded GPT answers "
        "no longer match the contexts this run sends; it needs re-recording on a live model"
    ),
)
def test_the_bundled_live_model_run_replays_offline_to_its_recorded_outputs(
    tmp_path: Path,
) -> None:
    """A real GPT run, with its failed calls, replays with no key and no network."""
    done = replay_run(RuntimeConfig(), LIVE, tmp_path / "replay")
    assert done.same, done.differing
    again = (tmp_path / "replay" / "outputs.json").read_bytes()
    assert again == (LIVE / "outputs.json").read_bytes()  # byte for byte
    manifest = json.loads((LIVE / "recording.json").read_text())
    assert done.run.outcome == manifest["outcome"]


def test_a_recording_that_does_not_match_the_run_is_refused_not_filled_in(
    tmp_path: Path,
) -> None:
    rec = tmp_path / "rec"
    shutil.copytree(EXAMPLE, rec)
    manifest = json.loads((rec / "recording.json").read_text())
    manifest["script"]["text"] = REQUEST + " Also compare battery life."
    (rec / "recording.json").write_text(json.dumps(manifest))
    done = replay_run(RuntimeConfig(), rec, tmp_path / "replay")
    assert not done.same and done.run.outcome != "complete"


def test_a_replay_runs_under_the_turn_factor_its_recording_ran_under(tmp_path: Path) -> None:
    rec = tmp_path / "rec"
    shutil.copytree(EXAMPLE, rec)
    manifest = json.loads((rec / "recording.json").read_text())
    manifest["turn_factors"] = {"research": 3}  # what a live recording's manifest keeps
    (rec / "recording.json").write_text(json.dumps(manifest))
    done = replay_run(RuntimeConfig(), rec, tmp_path / "replay")
    assert done.same, done.differing
    (store,) = (tmp_path / "replay" / "store").glob("*.sqlite")
    with closing(sqlite3.connect(store)) as db:
        (payload,) = db.execute("SELECT payload FROM events WHERE type = 'run.started'")
    started = json.loads(payload[0])
    assert started["turn_factor"] == 3 and started["budget"]["model_turns"] == 270


def test_recording_into_a_used_folder_and_replaying_nothing_are_refused(tmp_path: Path) -> None:
    (tmp_path / "used").mkdir()
    (tmp_path / "used" / "x").write_text("x")
    code, out = aksha(tmp_path, "run", REQUEST, "--agent", "research",
                      "--record", str(tmp_path / "used"))  # fmt: skip
    assert code == wire.EXIT_REFUSED and "recording_exists" in out, out
    code, out = aksha(tmp_path, "replay", str(tmp_path / "nothing"))
    assert code == wire.EXIT_REFUSED and "recording_missing" in out, out
    code, out = aksha(tmp_path, "run", REQUEST, "--record", str(tmp_path / "r2"))
    assert code == wire.EXIT_USAGE and "--agent" in out, out


def test_a_live_search_without_its_key_or_the_network_is_refused_before_any_call(
    tmp_path: Path,
) -> None:
    code, out = aksha(tmp_path, "run", REQUEST, "--agent", "research", "--record",
                      str(tmp_path / "r1"), AKSHA_SEARCH="brave")  # fmt: skip
    assert code == wire.EXIT_REFUSED and "search_key_missing" in out, out
    assert "AKSHA_BRAVE_API_KEY" in out
    code, out = aksha(tmp_path, "run", REQUEST, "--agent", "research", "--record",
                      str(tmp_path / "r2"), AKSHA_SEARCH="brave",
                      AKSHA_BRAVE_API_KEY="stub-key-for-tests")  # fmt: skip
    assert code == wire.EXIT_REFUSED and "network_off" in out, out
    code, out = aksha(tmp_path, "run", REQUEST, "--agent", "research", "--record",
                      str(tmp_path / "r3"), AKSHA_SEARCH="elsewhere")  # fmt: skip
    assert code == wire.EXIT_REFUSED and "search_invalid" in out, out
