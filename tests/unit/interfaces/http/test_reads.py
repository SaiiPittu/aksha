"""The reads router's shaping: pure functions from runtime values to the wire."""

from __future__ import annotations

import pytest

from aksha.interfaces import wire
from aksha.interfaces.http.reads import (
    InMemorySessionMeta,
    changes,
    default_title,
    document_to_json,
    node_id,
    parse_ref,
    session_status_name,
    session_to_json,
    upload_documents,
)
from aksha.runtime.artifacts.types import InputRef
from aksha.runtime.inputs.types import KnownDocument, StoredDocument
from aksha.runtime.session.views import RunStatus, SessionSummary, Waiting

S = "S-0001"


def _summary(latest: RunStatus | None, waiting: tuple[Waiting, ...] = ()) -> SessionSummary:
    runs = () if latest is None else (latest.run,)
    return SessionSummary(S, "Assess the four invention candidates", "t0", "t1", 9, runs,
                          latest, waiting)  # fmt: skip


def _run(state: str, outcome: str | None = None) -> RunStatus:
    return RunStatus("R-0001", "patent", S, state, outcome, False)  # type: ignore[arg-type]


def test_a_default_title_is_the_first_message_cut_to_80_characters() -> None:
    assert default_title("Short ask", S) == "Short ask"
    assert default_title("  spaced \n  out  ", S) == "spaced out"
    long = "word " * 40
    title = default_title(long, S)
    assert len(title) == 80 and title.endswith("…")
    assert default_title(None, S) == S and default_title("", S) == S


@pytest.mark.parametrize(
    ("latest", "waiting", "expected"),
    [
        (None, (), "idle"),
        (_run("running"), (), "running"),
        (_run("ended", "complete"), (), "done"),
        (_run("ended", "partial"), (), "partial"),
        (_run("ended", "stopped"), (), "stopped"),
        (_run("ended", "blocked"), (), "blocked"),
        (_run("ended", "something-new"), (), "failed"),
        (_run("ended", "complete"), (Waiting("I-0003", "review"),), "waiting"),
    ],
)
def test_a_sessions_status(latest: RunStatus | None, waiting: tuple[Waiting, ...],
                           expected: str) -> None:  # fmt: skip
    assert session_status_name(_summary(latest, waiting)) == expected


def test_a_session_on_the_wire_names_its_approval_by_its_approval_id() -> None:
    approval = Waiting("I-0002", "approval", approval_id="A-0001", signature="v1:x")
    out = session_to_json(_summary(_run("waiting"), (approval,)), title=None, archived=True)
    assert out["waiting"] == {"kind": "approval", "id": "A-0001"}
    assert out["title"] == "Assess the four invention candidates"
    assert out["archived"] is True and out["runs"] == 1 and out["run"] == "R-0001"
    asked = session_to_json(_summary(_run("waiting"), (Waiting("I-0001", "input"),)),
                            title="Mine", archived=False)  # fmt: skip
    assert asked["waiting"] == {"kind": "input", "id": "I-0001"} and asked["title"] == "Mine"
    idle = session_to_json(_summary(None), title=None, archived=False)
    assert idle["agent"] is None and idle["run"] is None and idle["waiting"] is None


def test_refs_parse_with_and_without_a_version() -> None:
    assert parse_ref("patent.packet/C1@v2") == ("patent.packet", "C1", 2)
    assert parse_ref("patent.portfolio@v1") == ("patent.portfolio", None, 1)
    assert parse_ref("patent.packet/C1") == ("patent.packet", "C1", None)
    assert parse_ref("patent.form/C1~a/b") == ("patent.form", "C1~a/b", None)
    for bad in ("patent.packet/C1@v0", "patent.packet@vx", "", "a b"):
        with pytest.raises(wire.WireInvalid):
            parse_ref(bad)


def test_a_lineage_node_is_an_artifact_ref_or_the_inputs_text() -> None:
    assert node_id(InputRef("artifact", "patent.packet/C1", "v2")) == "patent.packet/C1@v2"
    assert node_id(InputRef("record", "input/a.md", "sha256:x")) == "record:input/a.md@sha256:x"


def test_changes_are_json_pointer_adds_removes_and_replaces() -> None:
    before = {"title": "A", "rows": [{"x": 1}], "gone": True, "a/b": 1}
    after = {"title": "B", "rows": [{"x": 1}, {"x": 2}], "new": {"k": 1}, "a/b": 2}
    got = {c["pointer"]: (c["op"], c["before"], c["after"]) for c in changes(before, after)}
    assert got == {
        "/title": ("replace", "A", "B"),
        "/rows/1": ("add", None, {"x": 2}),
        "/gone": ("remove", True, None),
        "/new": ("add", None, {"k": 1}),
        "/a~1b": ("replace", 1, 2),
    }
    assert {c["op"] for c in changes(None, {"a": 1})} == {"add"}
    assert changes(after, after) == []


def test_a_document_lists_its_latest_version_and_every_version() -> None:
    facts = {"name": "records.csv", "detected_type": "text/csv", "size_bytes": 10,
             "origin": "upload"}  # fmt: skip
    versions = [StoredDocument("D-0001", v, "contributors/records.csv", f"b{v}", v, f"t{v}", facts)
                for v in (1, 2)]  # fmt: skip
    out = document_to_json(versions)
    assert out == {"doc_key": "D-0001", "path": "contributors/records.csv",
                   "name": "records.csv", "version": 2, "versions": [1, 2],
                   "media_type": "text/csv", "size": 10, "stored_at": "t2",
                   "origin": "upload"}  # fmt: skip


def test_an_uploads_documents_say_new_changed_unchanged_and_refused() -> None:
    after = (
        ("document.stored", {"path": "a.md", "doc_key": "D-0001", "version": 1,
                             "detected_type": "text/markdown", "size_bytes": 3}),
        ("document.stored", {"path": "b.md", "doc_key": "D-0002", "version": 3}),
        ("document.refused", {"name": "huge.pdf", "cap": "bytes"}),
    )  # fmt: skip
    known = {"c.md": KnownDocument("c.md", "D-0003", 1, "sha256:c")}
    got = {d["path"]: d["status"] for d in upload_documents(["a.md", "b.md", "c.md"], after, known)}
    assert got == {"a.md": "new", "b.md": "changed", "huge.pdf": "refused", "c.md": "unchanged"}


def test_the_in_memory_session_settings_are_per_owner() -> None:
    meta = InMemorySessionMeta()
    meta.set_archived("o1", S, True)
    meta.set_title("o1", S, "Mine")
    assert meta.archived("o1", S) and not meta.archived("o2", S)
    assert meta.title("o1", S) == "Mine" and meta.title("o2", S) is None
    meta.set_archived("o1", S, False)
    meta.set_title("o1", S, None)
    assert not meta.archived("o1", S) and meta.title("o1", S) is None
