"""Uploaded inputs through the session API and the CLI: versioned records, precise reopening.

An upload writes the owner's files and records what moved as one source change, so a file
uploaded again with other bytes reopens only the work built on it, and the session drives
the rebuild runs that opens. Both shipped agents run on a world in another domain
(``tilldew``) uploaded from its folder, the way a person would point the CLI at theirs.
"""

from __future__ import annotations

import io
import json
import re
import shutil
from pathlib import Path

from aksha import data
from aksha.app import DEFAULT_AGENTS, DEFAULT_EXTENSIONS
from aksha.interfaces import wire
from aksha.interfaces.cli.main import collect_inputs, main
from aksha.runtime.projections.view import load_view
from aksha.runtime.session import named as n
from aksha.runtime.session.views import run_statuses
from tests.support.session import env

TILLDEW = data.world_path("tilldew") / "sources"
REQUEST = (data.suites() / "tilldew" / "g-research-happy.toml").read_text("utf-8")
PROMPT = re.search(r'^opening = "(.*)"$', REQUEST, re.M).group(1)  # type: ignore[union-attr]


def aksha(tmp: Path, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    # The farm's own files are uploaded; the vendor pages and the prior art are on the
    # world's web, which AKSHA_SEARCH serves offline.
    extra = {"AKSHA_AGENTS": str(DEFAULT_AGENTS), "AKSHA_EXTENSIONS": str(DEFAULT_EXTENSIONS),
             "AKSHA_SEARCH": "world:tilldew"}  # fmt: skip
    code = main(["--user", "o1", *argv], out=out, env=env(tmp, **extra))
    return code, out.getvalue()


def _steps(tmp: Path, run: str) -> set[str]:
    """The Task instances a run took a step in."""
    from aksha.adapters.clock.fake import FakeClock
    from aksha.adapters.storage.sqlite import open_store
    from tests.support.events import START

    store = open_store(tmp / "var" / "aksha.sqlite", FakeClock(START), writer=False)
    try:
        return {
            str(e.step).split("/")[1]
            for e in store.read("o1")
            if e.type == "step.completed" and e.run == run
        }
    finally:
        store.close()


def _starts(tmp: Path) -> dict[str, dict[str, object]]:
    from aksha.adapters.clock.fake import FakeClock
    from aksha.adapters.storage.sqlite import open_store
    from aksha.runtime.canonical import thaw
    from tests.support.events import START

    store = open_store(tmp / "var" / "aksha.sqlite", FakeClock(START), writer=False)
    try:
        return {str(e.run): thaw(e.payload) for e in store.read("o1") if e.type == "run.started"}
    finally:
        store.close()


def test_collect_inputs_takes_a_folder_at_its_relative_paths_binaries_included(
    tmp_path: Path,
) -> None:
    (tmp_path / "data" / "sub").mkdir(parents=True)
    (tmp_path / "data" / "sub" / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "data" / ".hidden").write_text("h", encoding="utf-8")
    (tmp_path / "data" / "scan.pdf").write_bytes(b"%PDF\xff")
    (tmp_path / "one.csv").write_text("x\n", encoding="utf-8")
    files, skipped = collect_inputs([str(tmp_path / "data"), str(tmp_path / "one.csv")])
    assert files == {"sub/a.md": b"a", "scan.pdf": b"%PDF\xff", "one.csv": b"x\n"}
    assert skipped == []


def test_research_runs_on_an_uploaded_folder_and_a_changed_file_reopens_only_its_work(
    tmp_path: Path,
) -> None:
    data = tmp_path / "farm-data"
    shutil.copytree(TILLDEW, data)
    code, out = aksha(tmp_path, "run", PROMPT, "--agent", "research", "--input", str(data))
    assert code == wire.EXIT_OK, out
    assert "uploaded 14 input files" in out and "R-0001 research ended complete" in out
    first = _steps(tmp_path, "R-0001")
    assert {"scope", "discover", "voice", "report"} <= first

    code, out = aksha(tmp_path, "upload", str(data))  # nothing changed: nothing reopens
    assert code == wire.EXIT_OK and "no work reopened" in out, out
    code, out = aksha(tmp_path, "show")
    assert code == wire.EXIT_OK and "research.matrix@v1" in out.splitlines(), out
    code, out = aksha(tmp_path, "show", "research.needs")
    assert code == wire.EXIT_OK and '"phrase": "6 of 22 reviews in this sample"' in out, out

    reviews = data / "exports" / "farmer-reviews.csv"
    text = reviews.read_text(encoding="utf-8")
    reviews.write_text(text.replace("Too expensive for a two-acre plot.",
                                    "Too expensive, and the battery died."), "utf-8")  # fmt: skip
    code, out = aksha(tmp_path, "upload", str(data))
    assert code == wire.EXIT_OK, out
    assert "R-0002 research ended complete" in out
    # The review export is read by voice alone; the report reads voice's needs.
    assert _steps(tmp_path, "R-0002") == {"voice", "report"}
    starts = _starts(tmp_path)
    assert starts["R-0002"]["cause"] == "rebuild"
    assert starts["R-0002"]["budget"] == starts["R-0001"]["budget"]  # the agent's, not a default


def test_the_patent_agent_runs_on_the_same_uploaded_inputs_to_its_approval(
    tmp_path: Path,
) -> None:
    request = "Prepare Tilldew's invention candidates for counsel review."
    code, out = aksha(tmp_path, "run", request, "--agent", "patent", "--input", str(TILLDEW))
    assert code == wire.EXIT_WAITING_INPUT and "Tariff table source undecided" in out, out
    gap = re.search(r"aksha answer (I-\d+)", out)
    assert gap is not None
    code, out = aksha(tmp_path, "answer", gap.group(1), "The state utility's tariff table.")
    assert code == wire.EXIT_WAITING_INPUT and "TD-1: prioritize_counsel_review" in out, out
    choice = re.search(r"aksha answer (I-\d+)", out)
    assert choice is not None
    code, out = aksha(tmp_path, "answer", choice.group(1), "TD-1")
    assert code == wire.EXIT_WAITING_APPROVAL and "patent.packet/TD-1@v1" in out, out


def test_an_upload_through_the_session_api_is_refused_for_an_unclean_path(tmp_path: Path) -> None:
    from aksha.app import open_session
    from tests.support.session import config

    session = open_session(config(tmp_path, agent_paths=(DEFAULT_AGENTS,)))
    try:
        assert "UploadInputs" in session.commands()
        bad = session.dispatch(n.UploadInputs(owner="o1", files={"../x.md": "x"}))
        assert (bad.accepted, bad.refused) == (False, "upload_refused")
        ok = session.dispatch(n.UploadInputs(owner="o1", files={"a.md": "# A\n"}))
        assert ok.accepted
        again = session.dispatch(n.UploadInputs(owner="o1", files={"a.md": "# A\n"}))
        assert again.accepted and again.duplicate  # the same upload: its first receipt
        assert run_statuses(load_view(session.store, "o1")) == ()
    finally:
        session.store.close()


def test_an_upload_that_moves_a_file_back_is_recorded_even_with_a_derived_command_id(
    tmp_path: Path,
) -> None:
    """``A``, then ``B``, then ``A`` again is three changes, not a retry of the first one.

    Without a client command ID the command ID is derived from the upload's content, so the
    third upload derives the first one's ID; its bytes still moved the record on disk, so
    what it moved must be recorded, or work built on ``B`` would stay current over ``A``.
    """
    from aksha.app import open_session
    from aksha.runtime.canonical import thaw
    from tests.support.session import config

    code, out = aksha(tmp_path, "run", PROMPT, "--agent", "research", "--input", str(TILLDEW))
    assert code == wire.EXIT_OK, out
    path = "exports/farmer-reviews.csv"
    original = (TILLDEW / path).read_text(encoding="utf-8")
    edited = original.replace("Too expensive for a two-acre plot.", "Too expensive.")
    assert edited != original
    session = open_session(
        config(tmp_path, agent_paths=(DEFAULT_AGENTS,), extension_paths=(DEFAULT_EXTENSIONS,))
    )
    try:

        def changes() -> int:
            return sum(
                1
                for e in session.store.read("o1")
                if e.type == "source.changed" and f"input/{path}" in json.dumps(thaw(e.payload))
            )

        seen = []
        for text in (edited, original, edited):
            receipt = session.dispatch(n.UploadInputs(owner="o1", files={path: text}))
            assert receipt.accepted and not receipt.duplicate, receipt
            seen.append(changes())
        assert seen[0] >= 1 and seen[2] > seen[1], seen
        again = session.dispatch(n.UploadInputs(owner="o1", files={path: edited}))
        assert again.accepted and again.duplicate  # a true retry: nothing moved on disk
        assert changes() == seen[2]
    finally:
        session.store.close()


def test_files_written_without_their_record_are_recorded_by_recovery_and_reopen_work(
    tmp_path: Path,
) -> None:
    """A process that stops between writing an upload and recording it leaves the change
    unseen; recovery's source check records it, so the work built on the old file reopens.
    """
    from aksha.app import open_session
    from aksha.runtime.canonical import thaw
    from tests.support.session import config

    code, out = aksha(tmp_path, "run", PROMPT, "--agent", "research", "--input", str(TILLDEW))
    assert code == wire.EXIT_OK, out
    path = "exports/farmer-reviews.csv"
    edited = (
        (TILLDEW / path)
        .read_text(encoding="utf-8")
        .replace("Too expensive for a two-acre plot.", "Too expensive.")
    )

    def recovered() -> list[dict[str, object]]:
        return [
            thaw(e.payload)
            for e in session.store.read("o1")
            if e.type == "source.changed" and thaw(e.payload).get("cause") == "recovery"
        ]

    session = open_session(
        config(tmp_path, agent_paths=(DEFAULT_AGENTS,), extension_paths=(DEFAULT_EXTENSIONS,))
    )
    try:
        assert session.inputs is not None
        assert session.inputs.put("o1", {path: edited.encode("utf-8")})  # the crash: no record
        assert recovered() == []
        session.resume("o1")
        (change,) = recovered()
        assert f"input/{path}" in json.dumps(change)
        session.resume("o1")  # nothing moved since: nothing more is recorded
        assert len(recovered()) == 1
    finally:
        session.store.close()

    # Until a rebuild saves new versions, show marks what the change invalidated.
    code, out = aksha(tmp_path, "show")
    assert code == wire.EXIT_OK, out
    stale = [line for line in out.splitlines() if line.endswith("(stale: invalidated)")]
    assert any(line.startswith("research.needs@") for line in stale), out
    assert "research.scope@v1" in out.splitlines(), out  # the scope read no review row
    code, out = aksha(tmp_path, "show", "research.needs")
    assert code == wire.EXIT_OK and json.loads(out)["freshness"] == "invalidated", out


def test_a_server_session_records_unseen_input_files_before_the_owners_first_command(
    tmp_path: Path,
) -> None:
    from aksha.app import open_session
    from aksha.runtime.canonical import thaw
    from tests.support.session import config

    code, out = aksha(tmp_path, "run", PROMPT, "--agent", "research", "--input", str(TILLDEW))
    assert code == wire.EXIT_OK, out
    path = "exports/farmer-reviews.csv"
    edited = (
        (TILLDEW / path)
        .read_text(encoding="utf-8")
        .replace("Too expensive for a two-acre plot.", "Too expensive.")
    )
    session = open_session(
        config(tmp_path, agent_paths=(DEFAULT_AGENTS,), extension_paths=(DEFAULT_EXTENSIONS,))
    )
    try:
        assert session.inputs is not None
        session.inputs.put("o1", {path: edited.encode("utf-8")})
        receipt = session.dispatch(n.UploadInputs(owner="o1", files={"other.md": "# Other\n"}))
        assert receipt.accepted
        causes = [
            thaw(e.payload).get("cause")
            for e in session.store.read("o1")
            if e.type == "source.changed" and f"input/{path}" in json.dumps(thaw(e.payload))
        ]
        assert causes == ["recovery"], causes
    finally:
        session.store.close()
