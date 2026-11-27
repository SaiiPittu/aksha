"""The reads router over a real offline patent conversation (contract fixture recipe).

One module-scoped conversation, driven on the runtime thread exactly as ``aksha serve``
drives it: the stillroom world uploaded through ``POST /inputs/upload``, the patent
request, the input and choice answers, the approval, then the stillroom-v2 change set
uploaded again, which opens rebuild runs and a diff review that ``POST
/reviews/{review}/accept`` accepts. Snapshots taken along the way hold what the reads
returned at each point; the tests assert on them and on the final state.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from aksha import data
from aksha.app import DEFAULT_AGENTS, open_blob_store, open_reader, open_session
from aksha.interfaces.http.reads import InMemorySessionMeta, reads_router
from aksha.runtime.projections.view import load_view
from aksha.runtime.session import named as n
from aksha.runtime.session.streams import EventItem, Synchronized, subscribe
from aksha.runtime.session.views import RunStatus, SessionStatus, run_statuses, session_status
from aksha.runtime.session.worker import RuntimeThread
from aksha.runtime.storage.ports import EventStore
from tests.support.session import config

OWNER = "maya"
H = {"X-Aksha-User": OWNER}
OTHER = {"X-Aksha-User": "someone-else"}
SESSION = "S-ui-0001"
REQUEST = "Assess the four invention candidates and prepare the chosen one for counsel."
GAP = "0.5 m from the vent edge, so its air flow stays outside the footprint."
PACKET = "patent.packet/C1"
POLL_S = 0.2


def header_tenant(x_aksha_user: str = Header()) -> str:
    """The test tenant dependency: the owner is the header (the accounts layer replaces it)."""
    return x_aksha_user


def world_files(world: str) -> dict[str, str]:
    root = data.world_path(world) / "sources"
    return {p.relative_to(root).as_posix(): p.read_text("utf-8")
            for p in sorted(root.rglob("*")) if p.is_file()}  # fmt: skip


def multipart(files: Mapping[str, str | bytes]) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("file", (path, body if isinstance(body, bytes) else body.encode("utf-8"),
                      "application/octet-stream")) for path, body in files.items()]  # fmt: skip


@dataclass
class Conversation:
    client: TestClient
    runtime: RuntimeThread
    reader: Callable[[], EventStore]
    files: dict[str, str]
    changed: dict[str, str]
    snap: dict[str, Any] = field(default_factory=dict)

    def status(self) -> SessionStatus:
        store = self.reader()
        try:
            return session_status(load_view(store, OWNER), SESSION)
        finally:
            store.close()

    def runs(self) -> tuple[RunStatus, ...]:
        store = self.reader()
        try:
            return run_statuses(load_view(store, OWNER))
        finally:
            store.close()

    def submit(self, command: n.NamedCommand) -> None:
        receipt = self.runtime.submit(command).result(60)
        assert receipt.accepted, receipt

    def get(self, path: str, **params: Any) -> Any:
        r = self.client.get(f"/api/v1{path}", headers=H, params=params)
        assert r.status_code == 200, r.text
        return r.json()


def _wait[T](probe: Callable[[], T | None], what: str, timeout: float = 240.0) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = probe()
        if got:
            return got
        time.sleep(POLL_S)
    raise AssertionError(f"timed out waiting for {what}")


def _waiting_on(c: Conversation, kind: str, not_id: str | None = None) -> Any:
    def probe() -> Any:
        s = c.status()
        w = s.waiting_on[0] if s.yielded and s.waiting_on else None
        return w if w is not None and w.kind == kind and w.interrupt_id != not_id else None

    return _wait(probe, kind)


def _settled(c: Conversation, at_least: int) -> tuple[RunStatus, ...]:
    def probe() -> tuple[RunStatus, ...] | None:
        runs = c.runs()
        return runs if len(runs) >= at_least and all(r.state != "running" for r in runs) else None

    return _wait(probe, "runs to settle")


def _session_runs_on_stream(reader: Callable[[], EventStore]) -> list[str]:
    store = reader()
    try:
        out = []
        for item in subscribe(store, OWNER, SESSION, heartbeat_s=0.01):
            if isinstance(item, Synchronized):
                break
            if isinstance(item, EventItem) and item.event_type == "run.started":
                out.append(str(item.run))
        return out
    finally:
        store.close()


@pytest.fixture(scope="module")
def conv(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Conversation]:
    tmp = tmp_path_factory.mktemp("reads")
    cfg = config(tmp, agent_paths=(DEFAULT_AGENTS,),
                 systems={"counsel_mail": ("mailbox", tmp / "mailbox")})  # fmt: skip
    runtime = RuntimeThread(lambda: open_session(cfg))
    runtime.start()
    app = FastAPI()
    app.include_router(reads_router(lambda: open_reader(cfg), lambda: open_blob_store(cfg.storage),
                                    header_tenant, submit=runtime.submit,
                                    session_meta=InMemorySessionMeta()))  # fmt: skip
    files = world_files("stillroom")
    v2 = world_files("stillroom-v2")
    changed = {
        "publications/SYN-PUB-0011.json": v2["publications/SYN-PUB-0011.json"],
        "disclosures/consultant-email.eml.txt": files["disclosures/consultant-email.eml.txt"]
        .replace("Date: Mon, 05 Jan 2026 16:42:10 +0100", "Date: Fri, 21 Nov 2025 16:42:10 +0100"),
        "disclosures/history.csv": files["disclosures/history.csv"]
        .replace("DISC-05,2026-01-05,", "DISC-05,2025-11-21,"),
    }  # fmt: skip
    try:
        with TestClient(app) as client:
            c = Conversation(client, runtime, lambda: open_reader(cfg), files, changed)
            _drive(c)
            yield c
    finally:
        runtime.stop()


def _drive(c: Conversation) -> None:
    s = c.snap
    up = c.client.post("/api/v1/inputs/upload", headers=H, files=multipart(c.files),
                       data={"command_id": "c-upload-1"})  # fmt: skip
    s["upload_1"] = (up.status_code, up.json())
    c.submit(n.SendMessage(owner=OWNER, session=SESSION, text=REQUEST, agent="patent"))
    w = _waiting_on(c, "input")
    s["sessions_waiting"] = c.get("/sessions")
    c.submit(n.AnswerInterrupt(owner=OWNER, interrupt_id=w.interrupt_id, text=GAP))
    w = _waiting_on(c, "choice", not_id=w.interrupt_id)
    c.submit(n.AnswerInterrupt(owner=OWNER, interrupt_id=w.interrupt_id, text="C1"))
    w = _waiting_on(c, "approval")
    assert w.approval_id is not None and w.signature is not None
    c.submit(n.Approve(owner=OWNER, approval_id=w.approval_id, shown_signature=w.signature))
    _settled(c, 1)
    s["sessions_done"] = c.get("/sessions")
    s["artifacts_done"] = c.get("/artifacts")
    s["invalidation_done"] = c.get("/invalidation")

    same = next(p for p in sorted(c.files) if p not in c.changed)
    again = {**c.changed, same: c.files[same]}  # one path whose bytes did not move
    up = c.client.post("/api/v1/inputs/upload", headers=H, files=multipart(again))
    s["upload_2"] = (up.status_code, up.json())
    s["invalidation_after_upload"] = c.get("/invalidation")
    _settled(c, 2)
    s["stream_runs"] = _session_runs_on_stream(c.reader)
    s["reviews"] = c.get("/reviews")
    s["reviews_other_session"] = c.get("/reviews", session="S-nobody")
    s["sessions_review"] = c.get("/sessions")
    s["artifacts_review"] = c.get("/artifacts")
    review = s["reviews"]["reviews"][0]
    wrong = c.client.post(f"/api/v1/reviews/{review['review']}/accept", headers=H,
                          json={"card_hash": "sha256:" + "0" * 64})  # fmt: skip
    s["accept_wrong"] = (wrong.status_code, wrong.json())
    body = {"card_hash": review["card_hash"], "command_id": "c-review-1"}
    ok = c.client.post(f"/api/v1/reviews/{review['review']}/accept", headers=H, json=body)
    s["accept"] = (ok.status_code, ok.json())
    s["reviews_after_accept"] = c.get("/reviews")
    _settled(c, 3)


# --- uploads ---


def test_a_multipart_upload_returns_its_receipt_and_what_it_stored(conv: Conversation) -> None:
    status, body = conv.snap["upload_1"]
    assert status == 200, body
    assert body["receipt"]["status"] == "accepted"
    assert body["receipt"]["command_id"] == "c-upload-1"
    docs = body["documents"]
    assert {d["path"] for d in docs} == set(conv.files)
    assert {d["status"] for d in docs} == {"new"}
    assert all(d["doc_key"].startswith("D-") and d["version"] == 1 for d in docs)


def test_an_upload_again_says_what_is_new_changed_and_unchanged(conv: Conversation) -> None:
    status, body = conv.snap["upload_2"]
    assert status == 200, body
    by_path = {d["path"]: d for d in body["documents"]}
    assert by_path["publications/SYN-PUB-0011.json"]["status"] == "new"
    assert by_path["disclosures/history.csv"]["status"] == "changed"
    assert by_path["disclosures/history.csv"]["version"] == 2
    assert by_path["disclosures/consultant-email.eml.txt"]["status"] == "changed"
    unchanged = [d for d in body["documents"] if d["status"] == "unchanged"]
    assert len(unchanged) == len(by_path) - 3


def test_an_upload_with_no_file_part_is_refused(conv: Conversation) -> None:
    r = conv.client.post("/api/v1/inputs/upload", headers=H,
                         files=[("command_id", (None, b"c-x"))])  # fmt: skip
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation"


def test_an_upload_that_is_not_multipart_is_refused(conv: Conversation) -> None:
    r = conv.client.post("/api/v1/inputs/upload", headers=H, data={"command_id": "c-x"})
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_media_type"


# --- sessions ---


def test_the_session_list_names_the_title_status_and_runs(conv: Conversation) -> None:
    (waiting,) = conv.snap["sessions_waiting"]["sessions"]
    assert waiting["session"] == SESSION and waiting["title"] == REQUEST
    assert waiting["agent"] == "patent" and waiting["archived"] is False
    assert waiting["status"] == "waiting" and waiting["waiting"]["kind"] == "input"
    assert waiting["run"] == "R-0001" and waiting["runs"] == 1
    (done,) = conv.snap["sessions_done"]["sessions"]
    assert done["status"] == "done" and done["waiting"] is None
    assert done["started_at"] <= done["last_at"]
    (review,) = conv.snap["sessions_review"]["sessions"]
    # The rebuild runs belong to the session through for_runs, and the review waits there.
    assert review["runs"] >= 2 and review["run"] != "R-0001"
    assert review["status"] == "waiting" and review["waiting"]["kind"] == "review"
    assert conv.client.get("/api/v1/sessions", headers=OTHER).json() == {"sessions": []}


def test_archive_restore_and_retitle_a_session(conv: Conversation) -> None:
    client = conv.client
    r = client.post(f"/api/v1/sessions/{SESSION}/archive", headers=H)
    assert r.status_code == 200 and r.json()["session"]["archived"] is True
    assert conv.get("/sessions")["sessions"] == []
    assert [s["session"] for s in conv.get("/sessions", archived=1)["sessions"]] == [SESSION]
    r = client.post(f"/api/v1/sessions/{SESSION}/restore", headers=H)
    assert r.json()["session"]["archived"] is False
    r = client.patch(f"/api/v1/sessions/{SESSION}", headers=H, json={"title": " Counsel packet "})
    assert r.status_code == 200 and r.json()["session"]["title"] == "Counsel packet"
    r = client.patch(f"/api/v1/sessions/{SESSION}", headers=H, json={"title": None})
    assert r.json()["session"]["title"] == REQUEST
    assert (
        client.patch(f"/api/v1/sessions/{SESSION}", headers=H, json={"title": 3}).status_code == 422
    )
    assert client.post("/api/v1/sessions/S-nope/archive", headers=H).status_code == 404
    other = client.post(f"/api/v1/sessions/{SESSION}/archive", headers=OTHER)
    assert other.status_code == 404 and other.json()["error"]["code"] == "not_found"


def test_the_session_stream_carries_the_rebuild_runs(conv: Conversation) -> None:
    runs = conv.snap["stream_runs"]
    assert runs[0] == "R-0001" and "R-0002" in runs


# --- artifacts ---


def test_the_artifact_list_has_each_slot_once_at_its_current_version(conv: Conversation) -> None:
    arts = conv.snap["artifacts_done"]["artifacts"]
    refs = [a["ref"] for a in arts]
    assert len(refs) == len({(a["kind"], a["key"]) for a in arts})
    packet = next(a for a in arts if a["ref"].startswith(PACKET + "@"))
    assert packet["version"] == 1 and packet["versions"] == [1] and packet["current"] is True
    assert packet["session"] == SESSION and packet["run"] == "R-0001"
    assert packet["media_type"] == "application/json" and packet["size"] > 0
    assert {a["freshness"] for a in arts} == {"current"}
    seqs = [a["saved_at"] for a in arts]
    assert seqs == sorted(seqs, reverse=True)  # newest first
    # While the review is open, readers still see v1 of the packet, with v2 listed.
    during = next(a for a in conv.snap["artifacts_review"]["artifacts"]
                  if a["kind"] == "patent.packet")  # fmt: skip
    assert during["version"] == 1 and during["versions"] == [1, 2]
    after = next(a for a in conv.get("/artifacts")["artifacts"] if a["kind"] == "patent.packet")
    assert after["version"] == 2 and after["versions"] == [1, 2]


def test_the_artifact_list_filters_by_run_and_session(conv: Conversation) -> None:
    every = conv.get("/artifacts")["artifacts"]
    mine = conv.get("/artifacts", session=SESSION)["artifacts"]
    assert mine == every
    assert conv.get("/artifacts", session="S-nobody")["artifacts"] == []
    first = conv.get("/artifacts", run="R-0001")["artifacts"]
    assert first and {a["ref"] for a in first} <= {a["ref"] for a in every}
    assert conv.client.get("/api/v1/artifacts", headers=OTHER).json() == {"artifacts": []}


def test_one_artifact_with_its_content_reads_and_writer(conv: Conversation) -> None:
    latest = conv.get(f"/artifacts/{PACKET}")
    assert latest["ref"] == f"{PACKET}@v2" and latest["current"] is True
    assert latest["versions"] == [1, 2] and latest["freshness"] == "current"
    assert isinstance(latest["content"], dict) and latest["content"]
    assert latest["writer"]["agent"] == "patent" and latest["writer"]["task_path"]
    assert latest["reads"] and {"id", "kind", "version"} <= set(latest["reads"][0])
    assert {r["kind"] for r in latest["reads"]} >= {"artifact"}
    assert latest["citations"] == []  # the patent agent binds no citations
    old = conv.get(f"/artifacts/{PACKET}@v1")
    assert old["current"] is False and old["version"] == 1 and old["run"] == "R-0001"
    encoded = conv.get("/artifacts/" + quote(f"{PACKET}@v1", safe=""))
    assert encoded["ref"] == old["ref"]


def test_an_unknown_or_malformed_ref_and_another_owner(conv: Conversation) -> None:
    client = conv.client
    assert client.get(f"/api/v1/artifacts/{PACKET}@v9", headers=H).status_code == 404
    bad = client.get(f"/api/v1/artifacts/{PACKET}@vx", headers=H)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation"
    assert client.get(f"/api/v1/artifacts/{PACKET}", headers=OTHER).status_code == 404
    assert client.get(f"/api/v1/artifacts/{PACKET}/lineage", headers=OTHER).status_code == 404


def test_lineage_up_reaches_the_documents_and_down_reaches_the_packet(conv: Conversation) -> None:
    up = conv.get(f"/artifacts/{PACKET}@v1/lineage", dir="up", depth=3)
    ids = {node["id"] for node in up["nodes"]}
    assert up["root"] == f"{PACKET}@v1" and up["root"] in ids
    assert {node["type"] for node in up["nodes"]} >= {"artifact", "document"}
    assert all(e["from"] in ids and e["to"] in ids for e in up["edges"])
    assert any(e["to"] == up["root"] for e in up["edges"])
    shallow = conv.get(f"/artifacts/{PACKET}@v1/lineage", depth=1)
    assert len(shallow["nodes"]) <= len(up["nodes"])
    source = next(e["from"] for e in up["edges"] if e["to"] == up["root"]
                  and e["from"].startswith("patent."))  # fmt: skip
    down = conv.get(f"/artifacts/{source}/lineage", dir="down", depth=1)
    assert f"{PACKET}@v1" in {node["id"] for node in down["nodes"]}
    assert conv.client.get(f"/api/v1/artifacts/{PACKET}/lineage", headers=H,
                           params={"dir": "sideways"}).status_code == 422  # fmt: skip


def test_the_diff_of_two_versions_lists_pointer_changes(conv: Conversation) -> None:
    diff = conv.get(f"/artifacts/{PACKET}@v2/diff", against="@v1")
    assert diff["against"] == f"{PACKET}@v1"
    assert diff["before"] == conv.get(f"/artifacts/{PACKET}@v1")["content"]
    assert diff["after"] == conv.get(f"/artifacts/{PACKET}@v2")["content"]
    assert diff["changes"] and all(c["pointer"].startswith("/") for c in diff["changes"])
    assert {c["op"] for c in diff["changes"]} <= {"add", "remove", "replace"}
    assert conv.get(f"/artifacts/{PACKET}@v2/diff") == diff  # the previous version by default
    first = conv.get(f"/artifacts/{PACKET}@v1/diff")
    assert first["before"] is None and {c["op"] for c in first["changes"]} == {"add"}
    missing = conv.client.get(f"/api/v1/artifacts/{PACKET}@v2/diff", headers=H,
                              params={"against": "@v7"})  # fmt: skip
    assert missing.status_code == 404


# --- documents ---


def test_documents_list_their_versions_and_serve_their_bytes(conv: Conversation) -> None:
    docs = {d["path"]: d for d in conv.get("/documents")["documents"]}
    assert set(conv.files) <= set(docs)
    history = docs["disclosures/history.csv"]
    assert history["versions"] == [1, 2] and history["version"] == 2
    assert history["name"] == "history.csv" and history["origin"] == "upload"
    assert history["media_type"].startswith("text/") and history["size"] > 0
    latest = conv.client.get(f"/api/v1/documents/{history['doc_key']}", headers=H)
    assert latest.status_code == 200
    assert latest.text == conv.changed["disclosures/history.csv"]
    assert latest.headers["content-type"].startswith(history["media_type"])
    first = conv.client.get(f"/api/v1/documents/{history['doc_key']}", headers=H,
                            params={"version": 1})  # fmt: skip
    assert first.text == conv.files["disclosures/history.csv"]
    assert conv.client.get("/api/v1/documents/D-9999", headers=H).status_code == 404
    other = conv.client.get(f"/api/v1/documents/{history['doc_key']}", headers=OTHER)
    assert other.status_code == 404


# --- reviews and invalidation ---


def test_the_open_review_carries_its_card_hash_and_accepting_it_closes_it(
    conv: Conversation,
) -> None:
    (review,) = conv.snap["reviews"]["reviews"]
    assert review["interrupt"] == review["review"]
    assert review["old"] == f"{PACKET}@v1" and review["revision"] == f"{PACKET}@v2"
    assert review["card_hash"].startswith("sha256:") and review["changes"]
    assert conv.snap["reviews_other_session"] == {"reviews": []}
    status, body = conv.snap["accept_wrong"]
    assert status == 409 and body["error"]["code"] == "card_hash_mismatch"
    status, body = conv.snap["accept"]
    assert status == 200 and body["receipt"]["status"] == "accepted"
    assert conv.snap["reviews_after_accept"] == {"reviews": []}
    no_hash = conv.client.post(f"/api/v1/reviews/{review['review']}/reject", headers=H, json={})
    assert no_hash.status_code == 422


def test_invalidation_lists_stale_refs_and_open_rebuilds(conv: Conversation) -> None:
    keys = {"invalidated", "dirty", "current_by_cutoff", "noticed", "rebuilding"}
    done = conv.snap["invalidation_done"]
    assert set(done) == keys and done["invalidated"] == [] and done["rebuilding"] == []
    after = conv.snap["invalidation_after_upload"]
    assert after["invalidated"] or after["dirty"] or after["rebuilding"]
    final = conv.get("/invalidation")
    assert all(isinstance(final[k], list) for k in keys)


# --- a bare router over a fresh store ---


@pytest.fixture
def scribe(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient]]:
    """Two apps over one store: with the writer and session settings, and with neither.

    The shipped agents are loaded, because only they declare an inputs source to upload to.
    """
    cfg = config(tmp_path, agent_paths=(DEFAULT_AGENTS,))
    runtime = RuntimeThread(lambda: open_session(cfg))
    runtime.start()
    full, bare = FastAPI(), FastAPI()
    reader, blobs = (lambda: open_reader(cfg)), (lambda: open_blob_store(cfg.storage))
    full.include_router(reads_router(reader, blobs, header_tenant, submit=runtime.submit,
                                     session_meta=InMemorySessionMeta()))  # fmt: skip
    bare.include_router(reads_router(reader, blobs, header_tenant))
    try:
        with TestClient(full) as a, TestClient(bare) as b:
            yield a, b
    finally:
        runtime.stop()


def test_a_binary_part_goes_the_way_of_base64_and_reads_back_byte_for_byte(
    scribe: tuple[TestClient, TestClient],
) -> None:
    client, _ = scribe
    raw = bytes([0xFF, 0xFE, 0x00, 0x81]) * 64
    r = client.post("/api/v1/inputs/upload", headers=H,
                    files=multipart({"notes/a.md": "# A\n", "blobs/raw.bin": raw}))  # fmt: skip
    assert r.status_code == 200, r.text
    by_path = {d["path"]: d for d in r.json()["documents"]}
    assert by_path["notes/a.md"]["status"] == "new"
    stored = by_path["blobs/raw.bin"]
    assert stored["status"] == "new", r.text
    back = client.get(f"/api/v1/documents/{stored['doc_key']}", headers=H)
    assert back.status_code == 200 and back.content == raw
    text = client.get(f"/api/v1/documents/{by_path['notes/a.md']['doc_key']}", headers=H)
    assert text.text == "# A\n"
    assert {d["path"] for d in client.get("/api/v1/documents", headers=H).json()["documents"]} == {
        "notes/a.md", "blobs/raw.bin"}  # fmt: skip
    again = client.post("/api/v1/inputs/upload", headers=H, data={"command_id": "c-1"},
                        files=multipart({"notes/a.md": "# A\n"}))  # fmt: skip
    assert again.json()["documents"] == [
        {"path": "notes/a.md", "doc_key": by_path["notes/a.md"]["doc_key"], "version": 1,
         "status": "unchanged"}]  # fmt: skip


def test_without_a_writer_or_session_settings_the_writes_are_absent_or_501(
    scribe: tuple[TestClient, TestClient],
) -> None:
    _, bare = scribe
    assert bare.get("/api/v1/sessions", headers=H).json() == {"sessions": []}
    assert bare.post(f"/api/v1/sessions/{SESSION}/archive", headers=H).status_code == 501
    assert (
        bare.patch(f"/api/v1/sessions/{SESSION}", headers=H, json={"title": "x"}).status_code == 501
    )
    assert bare.post("/api/v1/inputs/upload", headers=H).status_code in (404, 405)
    assert bare.post("/api/v1/reviews/I-0001/accept", headers=H, json={}).status_code == 404
    assert bare.get("/api/v1/sessions").status_code == 422  # the tenant dependency holds


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("pages/x.html", "<html><body><script>fetch('/api/v1/auth/me')</script></body></html>"),
        (
            "pages/logo.svg",
            '<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        ),
    ],
)
def test_an_uploaded_document_is_sent_as_a_sandboxed_attachment_never_inline(
    scribe: tuple[TestClient, TestClient], path: str, body: str
) -> None:
    client, _ = scribe
    r = client.post("/api/v1/inputs/upload", headers=H, files=multipart({path: body}))
    assert r.status_code == 200, r.text
    (doc,) = r.json()["documents"]
    back = client.get(f"/api/v1/documents/{doc['doc_key']}", headers=H)
    assert back.status_code == 200 and back.text == body
    assert back.headers["x-content-type-options"] == "nosniff"
    assert back.headers["content-security-policy"] == "sandbox; default-src 'none'"
    name = path.rsplit("/", 1)[-1]
    assert back.headers["content-disposition"] == f"attachment; filename*=UTF-8''{name}"


def test_an_upload_over_the_cap_is_refused_too_large_and_nothing_is_stored(tmp_path: Path) -> None:
    cfg = config(tmp_path, agent_paths=(DEFAULT_AGENTS,))
    runtime = RuntimeThread(lambda: open_session(cfg))
    runtime.start()
    app = FastAPI()
    app.include_router(reads_router(lambda: open_reader(cfg), lambda: open_blob_store(cfg.storage),
                                    header_tenant, submit=runtime.submit,
                                    upload_max_bytes=1024))  # fmt: skip
    try:
        with TestClient(app) as client:
            r = client.post("/api/v1/inputs/upload", headers=H,
                            files=multipart({"notes/big.md": "x" * 2048}))  # fmt: skip
            assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"
            assert client.get("/api/v1/documents", headers=H).json() == {"documents": []}
            r = client.post("/api/v1/inputs/upload", headers=H,
                            files=multipart({"notes/small.md": "# small"}))  # fmt: skip
            assert r.status_code == 200, r.text
    finally:
        runtime.stop()
