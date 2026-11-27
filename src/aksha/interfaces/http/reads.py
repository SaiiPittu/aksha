"""The reads a client needs beside the session API: sessions, artifacts, documents, reviews.

:func:`reads_router` is an ``APIRouter`` the server includes. Every read opens a read-only
store handle, loads the owner's view and closes it, so it never boots and runs beside the
writer. The owner is whatever ``tenant_dep`` resolves (the accounts layer's principal, or
a header in tests); another owner's ID reads as unknown (ADR-P19 D9). Paths are under
``/api/v1``; every list is newest first:

=======  ==========================================  =========================================
Method   Path                                        What
=======  ==========================================  =========================================
GET      ``/sessions?archived=0|1``                  every session: title, status, its runs
POST     ``/sessions/{session}/archive|restore``     archive state (``SessionMetaPort``)
PATCH    ``/sessions/{session}``                     ``{"title": text or null}``
GET      ``/artifacts?session=&run=``                each slot's current version
GET      ``/artifacts/{ref}``                        one version (``kind[/key][@vN]``) in full
GET      ``/artifacts/{ref}/lineage``                ``?dir=up|down&depth=3``
GET      ``/artifacts/{ref}/diff``                   ``?against=@vN`` (default: the previous)
GET      ``/documents``                              each uploaded document's latest version
GET      ``/documents/{doc_key}``                    its bytes (``?version=N``)
POST     ``/inputs/upload``                          multipart ``file`` parts: ``UploadInputs``
GET      ``/reviews?session=``                       the open diff reviews, with ``card_hash``
POST     ``/reviews/{review}/accept|reject``         ``AcceptReview`` / ``RejectReview``
GET      ``/invalidation``                           stale refs and the rebuild runs still open
=======  ==========================================  =========================================

A session's runs include the rebuild runs opened for them (``run_sessions``). The archive
state and a title override are server-layer settings behind :class:`SessionMetaPort`;
without one every session reads unarchived with its default title and the three writes
answer 501. The write endpoints (upload, accept, reject) exist only when ``submit`` is
given: it queues a command on the runtime thread, the single writer.
"""

from __future__ import annotations

import asyncio
import base64
import functools
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future
from contextlib import contextmanager
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import UploadFile

from aksha.interfaces import wire
from aksha.interfaces.http.common import (
    MB,
    MULTIPART_MEDIA,
    UPLOAD_MAX_MB,
    RequestRefused,
    json_body,
    read_body,
    replayed,
    require_media,
)
from aksha.interfaces.http.ports import ByBinder, SessionMetaPort
from aksha.runtime.artifacts.lineage import lineage
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactKind, ArtifactRef, ArtifactVersion, InputRef
from aksha.runtime.artifacts.versions import all_versions, current, version, versions
from aksha.runtime.canonical import canonical_json, thaw
from aksha.runtime.errors import AkshaError
from aksha.runtime.grounding.facts import bound_citations
from aksha.runtime.grounding.spans import span_index
from aksha.runtime.inputs.reducers import documents_after, known_documents, stored_documents
from aksha.runtime.inputs.types import DOCUMENT_STORED, StoredDocument
from aksha.runtime.interrupts.reducers import interrupts
from aksha.runtime.invalidation.redline import redline
from aksha.runtime.invalidation.reviews import open_reviews
from aksha.runtime.invalidation.view import InvalidationView, invalidation_view, runs_for
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.session import named
from aksha.runtime.session.errors import CommandIdReused
from aksha.runtime.session.types import CommandReceipt
from aksha.runtime.session.views import SessionSummary, run_sessions, run_statuses
from aksha.runtime.session.views import session_summaries as summaries
from aksha.runtime.storage.ports import EventStore

API = "/api/v1"
COMMAND_TIMEOUT_S = 60.0
"""How long a write waits for its command to commit before answering 504."""

TITLE_CHARS = 80
"""A default title is the session's first message cut to this many characters."""

MAX_DEPTH = 20
"""The deepest lineage a request may ask for."""

MAX_CHANGES = 10_000
"""The most changes one diff lists."""

JSON_MEDIA = "application/json"

Submit = Callable[[named.NamedCommand], "Future[CommandReceipt]"]
"""Queue a command on the single writer; the future holds its receipt (``RuntimeThread``)."""

SessionStatusName = Literal[
    "waiting", "running", "done", "failed", "blocked", "partial", "stopped", "idle"
]

ENDED_STATUS: Mapping[str, SessionStatusName] = {
    "complete": "done",
    "partial": "partial",
    "stopped": "stopped",
    "blocked": "blocked",
    "failed": "failed",
}
"""An ended run's outcome to its session's status (an unknown outcome reads ``failed``)."""

NODE_TYPES: Mapping[str, str] = {
    "artifact": "artifact",
    "record": "document",
    "blob": "document",
    "query": "query",
    "statement": "statement",
    "time": "time",
}
"""A lineage input's kind to the node type a client draws."""


class InMemorySessionMeta:
    """A :class:`SessionMetaPort` in a dict: for tests and a server with no accounts store."""

    def __init__(self) -> None:
        """Start with nothing archived and no titles."""
        self._archived: set[tuple[str, str]] = set()
        self._titles: dict[tuple[str, str], str] = {}

    def archived(self, owner: str, session: str) -> bool:
        """Whether ``owner`` archived ``session``."""
        return (owner, session) in self._archived

    def set_archived(self, owner: str, session: str, archived: bool) -> None:
        """Archive or restore ``session`` for ``owner``."""
        if archived:
            self._archived.add((owner, session))
        else:
            self._archived.discard((owner, session))

    def title(self, owner: str, session: str) -> str | None:
        """The title ``owner`` gave ``session``."""
        return self._titles.get((owner, session))

    def set_title(self, owner: str, session: str, title: str | None) -> None:
        """Set or clear ``session``'s title for ``owner``."""
        if title is None:
            self._titles.pop((owner, session), None)
        else:
            self._titles[(owner, session)] = title


# --- shaping: pure functions from runtime values to the wire --------------------------


def error(status: int, code: str, message: str, details: object = None) -> JSONResponse:
    """The one error shape: ``{"error": {"code", "message", "details"}}``."""
    body = {"error": {"code": code, "message": message, "details": details}}
    return JSONResponse(body, status_code=status)


def default_title(first_message: str | None, session: str) -> str:
    """The first message cut to :data:`TITLE_CHARS`, or the session ID when there is none."""
    if not first_message:
        return session
    text = " ".join(first_message.split())
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1] + "…"


def session_status_name(summary: SessionSummary) -> SessionStatusName:
    """``waiting`` while anything is open, else where the session's latest run stands."""
    if summary.waiting_on:
        return "waiting"
    latest = summary.latest
    if latest is None:
        return "idle"
    if latest.state != "ended":
        return "running"
    return ENDED_STATUS.get(latest.outcome or "", "failed")


def session_to_json(
    summary: SessionSummary, *, title: str | None, archived: bool
) -> dict[str, Any]:
    """The wire form of one session in ``GET /sessions``."""
    first = summary.waiting_on[0] if summary.waiting_on else None
    waiting = None
    if first is not None:
        ident = first.approval_id if first.kind == "approval" and first.approval_id else None
        waiting = {"kind": first.kind, "id": ident or first.interrupt_id}
    latest = summary.latest
    return {
        "session": summary.session,
        "title": title or default_title(summary.first_message, summary.session),
        "agent": None if latest is None else latest.agent,
        "started_at": summary.started_at,
        "last_at": summary.last_at,
        "status": session_status_name(summary),
        "waiting": waiting,
        "run": None if latest is None else latest.run,
        "runs": len(summary.runs),
        "archived": archived,
    }


def parse_ref(text: str) -> tuple[str, str | None, int | None]:
    """``kind[/key][@vN]`` as ``(kind, key, version)``; ``WireInvalid`` for anything else."""
    if "@" in text:
        try:
            ref = ArtifactRef.parse(text)
        except ValueError as e:
            raise wire.WireInvalid(message=str(e)) from None
        return ref.kind, ref.key, ref.version
    kind, _, key = text.partition("/")
    if not kind or any(c.isspace() for c in text):
        raise wire.WireInvalid(message=f"not an artifact ref: {text!r}")
    return kind, key or None, None


def input_to_json(read: InputRef) -> dict[str, str]:
    """A lineage input as ``{"id", "kind", "version"}`` (and ``path`` when path-scoped)."""
    out = {"id": read.id, "kind": read.kind, "version": read.version}
    if read.path is not None:
        out["path"] = read.path
    return out


def node_id(read: InputRef) -> str:
    """A lineage node's ID: an artifact's ref (``kind/key@vN``), else the input's text."""
    if read.kind == "artifact":
        return str(ArtifactRef.from_input(read))
    return str(read)


def changes(before: Any, after: Any) -> list[dict[str, Any]]:
    """The JSON-pointer changes from ``before`` to ``after``: each an add, remove or replace.

    The runtime's field-level redline does the walk (objects key by key, lists by
    position); an absent side is ``None``.
    """
    if not isinstance(before, Mapping):
        before = {}
    hunks = redline(ArtifactKind("diff", {}), before, after, max_hunks=MAX_CHANGES)
    out: list[dict[str, Any]] = []
    for h in hunks:
        op = "add" if h.old is None else "remove" if h.new is None else "replace"
        out.append({"pointer": h.path, "op": op, "before": thaw(h.old), "after": thaw(h.new)})
    return out


def document_to_json(doc_versions: list[StoredDocument]) -> dict[str, Any]:
    """One document in ``GET /documents``: its latest version and every version number."""
    latest = doc_versions[-1]
    facts = latest.facts
    return {
        "doc_key": latest.doc_key,
        "path": latest.path,
        "name": str(facts.get("name") or latest.path.rsplit("/", 1)[-1]),
        "version": latest.version,
        "versions": [d.version for d in doc_versions],
        "media_type": facts.get("detected_type"),
        "size": facts.get("size_bytes"),
        "stored_at": latest.at,
        "origin": facts.get("origin"),
    }


DOCUMENT_CSP = "sandbox; default-src 'none'"
"""The policy every document's bytes are sent under: no script, no fetch, no same origin."""


def document_headers(path: str) -> dict[str, str]:
    """The headers of a document's bytes: an attachment, never sniffed, sandboxed.

    An uploaded HTML or SVG file served inline from the app's own origin would run its
    script with the reader's cookie (stored XSS). Sent as an attachment with ``nosniff``
    and a ``sandbox`` policy, it never renders as a page, and nothing the web app does needs
    it inline: it reads a text document with ``fetch`` and shows it as text, shows an image
    with ``<img>`` (which ignores the disposition, and the policy does not apply to it), and
    fetches a PDF's bytes into a ``Blob`` it types ``application/pdf`` itself, framing the
    object URL for the browser's PDF viewer, never this URL.
    """
    name = path.rsplit("/", 1)[-1] or "document"
    return {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": DOCUMENT_CSP,
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}",
    }


def upload_documents(
    paths: list[str],
    after: tuple[tuple[str, Mapping[str, object]], ...],
    known: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """What an upload stored, per path: ``new``, ``changed``, ``unchanged`` or a refusal.

    ``after`` holds the ``document.*`` facts right after the receipt (attachments among
    them); a path with none kept its bytes, so its known document is ``unchanged``.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for kind, p in after:
        path = str(p.get("path") or p.get("name") or "")
        seen.add(path)
        entry: dict[str, Any] = {
            "path": path,
            "doc_key": p.get("doc_key"),
            "version": p.get("version"),
        }
        if kind == DOCUMENT_STORED:
            entry["status"] = "new" if p.get("version") == 1 else "changed"
            entry["media_type"] = p.get("detected_type")
            entry["size"] = p.get("size_bytes")
        else:
            entry["status"] = kind.rpartition(".")[2]  # gap or refused
        out.append(entry)
    for path in paths:
        doc = known.get(path)
        if path not in seen and doc is not None:
            out.append(
                {
                    "path": path,
                    "doc_key": doc.doc_key,
                    "version": doc.version,
                    "status": "unchanged",
                }
            )
    return out


# --- the router -----------------------------------------------------------------------


def reads_router(
    open_reader: Callable[[], EventStore],
    open_blobs: Callable[[], BlobStore],
    tenant_dep: Callable[..., str],
    *,
    submit: Submit | None = None,
    session_meta: SessionMetaPort | None = None,
    command_tenant_dep: Callable[..., str] | None = None,
    command_timeout_s: float = COMMAND_TIMEOUT_S,
    upload_max_bytes: int = UPLOAD_MAX_MB * MB,
    principal_by_dep: Callable[..., ByBinder] | None = None,
) -> APIRouter:
    """The reads router (module doc) over a read-only store and the blob store.

    ``tenant_dep`` is a FastAPI dependency that returns the owner a read reads;
    ``command_tenant_dep`` (default: ``tenant_dep``) returns the owner a write acts for, so
    the accounts layer can refuse a Viewer and an Admin's ``as_user`` on writes. ``submit`` (usually
    ``RuntimeThread.submit``) enables the three writes; ``session_meta`` the archive state
    and title overrides. An upload over ``upload_max_bytes`` is refused 413 ``too_large``.
    ``principal_by_dep`` returns the :class:`ByBinder` that sets a write's ``by`` from its
    sender (``auth.principal_by``); without one the body's ``by`` is kept.
    """
    command_dep = command_tenant_dep or tenant_dep
    by_dep = principal_by_dep or _keep_by
    router = APIRouter(prefix=API)
    meta = session_meta

    @contextmanager
    def reading(owner: str) -> Iterator[LogView]:
        store = open_reader()
        try:
            yield load_view(store, owner)
        finally:
            store.close()

    def stale(view: LogView, blobs: BlobStore) -> InvalidationView:
        return invalidation_view(view, blobs=blobs)

    def freshness(iv: InvalidationView, ref: ArtifactRef) -> str:
        state = iv.versions.get(str(ref))
        return "current" if state is None else str(state.freshness)

    def saved_at(view: LogView) -> dict[int, str]:
        return {e.seq: e.at for e in view.events if e.type == "artifact.saved"}

    def session_known(owner: str, session: str) -> SessionSummary | None:
        with reading(owner) as view:
            return next((s for s in summaries(view) if s.session == session), None)

    # --- sessions ---

    def one_session(owner: str, summary: SessionSummary) -> dict[str, Any]:
        title = None if meta is None else meta.title(owner, summary.session)
        archived = False if meta is None else meta.archived(owner, summary.session)
        return session_to_json(summary, title=title, archived=archived)

    @router.get("/sessions")
    def list_sessions(
        owner: str = Depends(tenant_dep), archived: Annotated[int, Query(ge=0, le=1)] = 0
    ) -> dict[str, Any]:
        with reading(owner) as view:
            found = sorted(summaries(view), key=lambda s: s.last_seq, reverse=True)
        items = [one_session(owner, s) for s in found]
        return {"sessions": [s for s in items if s["archived"] is bool(archived)]}

    def set_meta(
        owner: str, session: str, change: Callable[[SessionMetaPort], None]
    ) -> JSONResponse:
        if meta is None:
            return error(501, "not_supported", "this server keeps no session settings")
        summary = session_known(owner, session)
        if summary is None:
            return error(404, "not_found", f"no session {session}")
        change(meta)
        return JSONResponse({"session": one_session(owner, summary)})

    @router.post("/sessions/{session}/archive")
    def archive(session: str, owner: str = Depends(command_dep)) -> JSONResponse:
        return set_meta(owner, session, lambda m: m.set_archived(owner, session, True))

    @router.post("/sessions/{session}/restore")
    def restore(session: str, owner: str = Depends(command_dep)) -> JSONResponse:
        return set_meta(owner, session, lambda m: m.set_archived(owner, session, False))

    @router.patch("/sessions/{session}")
    async def retitle(
        session: str, request: Request, owner: str = Depends(command_dep)
    ) -> JSONResponse:
        try:
            body = await json_body(request)
        except wire.WireInvalid as e:
            return error(422, e.code, e.message)
        except RequestRefused as e:
            return error(e.status, e.code, e.message)
        title = body.get("title")
        if "title" not in body or not (title is None or isinstance(title, str)):
            return error(422, "validation", "title is a string, or null for the default")
        text = None if title is None or not title.strip() else title.strip()
        return await run_in_threadpool(
            set_meta, owner, session, lambda m: m.set_title(owner, session, text)
        )

    # --- artifacts ---

    def slot_entry(
        view: LogView,
        v: ArtifactVersion,
        iv: InvalidationView,
        at: Mapping[int, str],
        belongs: Mapping[str, frozenset[str]],
    ) -> dict[str, Any]:
        content = thaw(v.content)
        sessions = sorted(belongs.get(v.run or "", ()))
        return {
            "ref": str(v.ref),
            "kind": v.ref.kind,
            "key": v.ref.key,
            "version": v.ref.version,
            "versions": [x.ref.version for x in versions(view, v.ref.kind, v.ref.key)],
            "current": True,
            "freshness": freshness(iv, v.ref),
            "run": v.run,
            "session": sessions[0] if sessions else None,
            "saved_at": at.get(v.event_seq),
            "media_type": JSON_MEDIA,
            "size": len(canonical_json(content).encode("utf-8")),
        }

    @router.get("/artifacts")
    def list_artifacts(
        owner: str = Depends(tenant_dep), session: str | None = None, run: str | None = None
    ) -> dict[str, Any]:
        blobs = open_blobs()
        with reading(owner) as view:
            belongs = run_sessions(view)
            iv = stale(view, blobs)
            at = saved_at(view)
            slots: dict[tuple[str, str | None], bool] = {}
            for v in all_versions(view):
                wanted = (run is None or v.run == run) and (
                    session is None or session in belongs.get(v.run or "", ())
                )
                slots[v.ref.slot()] = slots.get(v.ref.slot(), False) or wanted
            found: list[ArtifactVersion] = []
            for (kind, key), wanted in slots.items():
                cur = current(view, kind, key, blobs=blobs) if wanted else None
                if cur is not None:
                    found.append(cur)
            found.sort(key=lambda v: v.event_seq, reverse=True)
            return {"artifacts": [slot_entry(view, v, iv, at, belongs) for v in found]}

    def resolve(view: LogView, text: str, blobs: BlobStore) -> ArtifactVersion | None:
        kind, key, number = parse_ref(text)
        if number is None:
            return current(view, kind, key, blobs=blobs)
        return version(view, ArtifactRef(kind, key, number), blobs=blobs)

    @router.get("/artifacts/{ref:path}/lineage", response_model=None)
    @_invalid_as_422
    def artifact_lineage(
        ref: str,
        owner: str = Depends(tenant_dep),
        dir: Literal["up", "down"] = "up",
        depth: Annotated[int, Query(ge=1, le=MAX_DEPTH)] = 3,
    ) -> dict[str, Any] | JSONResponse:
        blobs = open_blobs()
        with reading(owner) as view:
            found = resolve(view, ref, blobs)
            if found is None:
                return error(404, "not_found", f"no artifact {ref}")
            graph = lineage(view, found.ref, direction=dir, depth=depth, blobs=blobs)
            iv = stale(view, blobs)
        nodes = []
        for n in sorted(graph.nodes, key=InputRef.sort_key):
            is_artifact = n.kind == "artifact"
            nodes.append(
                {
                    "id": node_id(n),
                    "type": NODE_TYPES.get(n.kind, n.kind),
                    "label": node_id(n) if is_artifact else n.id,
                    "freshness": freshness(iv, ArtifactRef.from_input(n)) if is_artifact else None,
                }
            )
        edges = sorted({(node_id(a), node_id(b)) for a, b in graph.edges})
        return {
            "root": str(found.ref),
            "direction": dir,
            "nodes": nodes,
            "edges": [{"from": a, "to": b} for a, b in edges],
        }

    @router.get("/artifacts/{ref:path}/diff", response_model=None)
    @_invalid_as_422
    def artifact_diff(
        ref: str, owner: str = Depends(tenant_dep), against: str | None = None
    ) -> dict[str, Any] | JSONResponse:
        blobs = open_blobs()
        with reading(owner) as view:
            after = resolve(view, ref, blobs)
            if after is None:
                return error(404, "not_found", f"no artifact {ref}")
            if against is None:
                number = after.ref.version - 1
                base = None if number < 1 else ArtifactRef(after.ref.kind, after.ref.key, number)
            else:
                text = f"{after.ref.slot_id()}{against}" if against.startswith("@") else against
                kind, key, number_given = parse_ref(text)
                if number_given is None:
                    raise wire.WireInvalid(message="against names a version: @vN or kind/key@vN")
                base = ArtifactRef(kind, key, number_given)
            before = None if base is None else version(view, base, blobs=blobs)
            if base is not None and before is None:
                return error(404, "not_found", f"no artifact {base}")
        old = None if before is None else thaw(before.content)
        new = thaw(after.content)
        return {
            "ref": str(after.ref),
            "against": None if before is None else str(before.ref),
            "before": old,
            "after": new,
            "changes": changes(old, new),
        }

    @router.get("/artifacts/{ref:path}", response_model=None)
    @_invalid_as_422
    def artifact(ref: str, owner: str = Depends(tenant_dep)) -> dict[str, Any] | JSONResponse:
        blobs = open_blobs()
        with reading(owner) as view:
            found = resolve(view, ref, blobs)
            if found is None:
                return error(404, "not_found", f"no artifact {ref}")
            cur = current(view, found.ref.kind, found.ref.key)
            iv = stale(view, blobs)
            spans = span_index(view)
            cites = []
            for c in bound_citations(view, str(found.ref)):
                span = spans.get(str(c.get("target"))) if c.get("form") == "span" else None
                cites.append(
                    {
                        "n": c.get("n"),
                        "pointer": c.get("pointer"),
                        "quote": c.get("quote"),
                        "target": c.get("target"),
                        "form": c.get("form"),
                        "record_id": None if span is None else span.record_id,
                    }
                )
            entry = {
                "ref": str(found.ref),
                "kind": found.ref.kind,
                "key": found.ref.key,
                "version": found.ref.version,
                "versions": [x.ref.version for x in versions(view, found.ref.kind, found.ref.key)],
                "current": cur is not None and cur.ref == found.ref,
                "media_type": JSON_MEDIA,
                "content": thaw(found.content),
                "run": found.run,
                "writer": {"agent": found.writer.agent, "task_path": found.writer.task_path},
                "freshness": freshness(iv, found.ref),
                "saved_at": saved_at(view).get(found.event_seq),
                "reads": [input_to_json(r) for r in found.reads],
                "primary_inputs": [str(r) for r in found.primary_inputs],
                "citations": cites,
            }
        return entry

    # --- documents ---

    def by_key(view: LogView) -> dict[str, list[StoredDocument]]:
        out: dict[str, list[StoredDocument]] = {}
        for d in stored_documents(view):
            out.setdefault(d.doc_key, []).append(d)
        return out

    @router.get("/documents")
    def list_documents(owner: str = Depends(tenant_dep)) -> dict[str, Any]:
        with reading(owner) as view:
            docs = by_key(view)
        ordered = sorted(docs.values(), key=lambda vs: vs[-1].seq, reverse=True)
        return {"documents": [document_to_json(vs) for vs in ordered]}

    @router.get("/documents/{doc_key}")
    def document(
        doc_key: str, owner: str = Depends(tenant_dep), version: int | None = None
    ) -> Response:
        with reading(owner) as view:
            found = by_key(view).get(doc_key, [])
        pick = (
            found[-1]
            if found and version is None
            else next((d for d in found if d.version == version), None)
        )
        if pick is None:
            return error(404, "not_found", f"no document {doc_key}")
        try:
            data = open_blobs().get(pick.blob)
        except AkshaError as e:
            return error(404, e.code, e.message)
        media = str(pick.facts.get("detected_type") or "application/octet-stream")
        return Response(data, media_type=media, headers=document_headers(pick.path))

    # --- reviews and invalidation ---

    @router.get("/reviews")
    def list_reviews(
        owner: str = Depends(tenant_dep), session: str | None = None
    ) -> dict[str, Any]:
        with reading(owner) as view:
            belongs = run_sessions(view)
            raised = interrupts(view)
            out = []
            for r in sorted(open_reviews(view).values(), key=lambda r: r.seq, reverse=True):
                if session is not None and session not in belongs.get(r.run or "", ()):
                    continue
                payload = thaw(raised[r.interrupt].payload) if r.interrupt in raised else {}
                out.append(
                    {
                        "review": r.interrupt,
                        "interrupt": r.interrupt,
                        "old": str(r.old),
                        "revision": str(r.revision),
                        "card_hash": r.card_hash,
                        "run": r.run,
                        "instance": r.instance,
                        "changes": payload.get("hunks", []),
                        "changed_reads": payload.get("changed_reads", []),
                    }
                )
        return {"reviews": out}

    @router.get("/invalidation")
    def invalidation(owner: str = Depends(tenant_dep)) -> dict[str, Any]:
        blobs = open_blobs()
        with reading(owner) as view:
            iv = stale(view, blobs)
            lines = runs_for(view)
            rebuilding = [
                s.run
                for s in run_statuses(view)
                if s.state != "ended" and lines.get(s.run, frozenset({s.run})) != {s.run}
            ]
        return {
            "invalidated": list(iv.refs("invalidated")),
            "dirty": list(iv.refs("dirty")),
            "current_by_cutoff": list(iv.refs("current_by_cutoff")),
            "noticed": list(iv.noticed()),
            "rebuilding": rebuilding,
        }

    if submit is None:
        return router

    # --- writes through the single writer ---

    async def commit(command: named.NamedCommand) -> CommandReceipt | JSONResponse:
        try:
            future = submit(command)
            receipt = await asyncio.wait_for(asyncio.wrap_future(future), command_timeout_s)
        except CommandIdReused as e:
            return error(409, e.code, e.message)
        except AkshaError as e:
            return error(409, e.code, e.message)
        except TimeoutError:
            return error(504, "timeout", "the command did not commit in time")
        if not receipt.accepted:
            return error(
                wire.refusal_status(receipt.refused),
                receipt.refused or "refused",
                f"{type(command).__name__} refused",
                wire.receipt_to_json(receipt),
            )
        return receipt

    def parsed(
        owner: str, name: str, args: dict[str, Any], body: Mapping[str, Any], by: ByBinder
    ) -> named.NamedCommand:
        return by.bind(
            wire.command_from_json(
                owner,
                {
                    "command": name,
                    "args": args,
                    "command_id": body.get("command_id"),
                    "by": body.get("by"),
                },
            )
        )

    async def review_command(
        name: str, review: str, request: Request, owner: str, by: ByBinder
    ) -> JSONResponse:
        try:
            body = await json_body(request)
        except wire.WireInvalid as e:
            return error(422, e.code, e.message)
        except RequestRefused as e:
            return error(e.status, e.code, e.message)
        if "edits" in body:
            return error(422, "validation", "edits are not supported: accept, then correct")
        card = body.get("card_hash")
        if not isinstance(card, str) or not card:
            return error(422, "validation", "card_hash is the hash of the card the person saw")
        try:
            command = parsed(owner, name, {"review_id": review, "card_hash": card}, body, by)
        except wire.WireInvalid as e:
            return error(422, e.code, e.message)
        receipt = await commit(command)
        if isinstance(receipt, JSONResponse):
            return receipt
        return JSONResponse({"receipt": wire.receipt_to_json(receipt)})

    @router.post("/reviews/{review}/accept")
    async def accept(
        review: str,
        request: Request,
        owner: str = Depends(command_dep),
        by: ByBinder = Depends(by_dep),  # noqa: B008 (a FastAPI dependency, resolved per request)
    ) -> JSONResponse:
        return await review_command("AcceptReview", review, request, owner, by)

    @router.post("/reviews/{review}/reject")
    async def reject(
        review: str,
        request: Request,
        owner: str = Depends(command_dep),
        by: ByBinder = Depends(by_dep),  # noqa: B008 (a FastAPI dependency, resolved per request)
    ) -> JSONResponse:
        return await review_command("RejectReview", review, request, owner, by)

    @router.post("/inputs/upload")
    async def upload(
        request: Request,
        owner: str = Depends(command_dep),
        by: ByBinder = Depends(by_dep),  # noqa: B008 (a FastAPI dependency, resolved per request)
    ) -> JSONResponse:
        files: dict[str, str] = {}
        binary: dict[str, str] = {}
        try:
            raw = await read_body(request, upload_max_bytes)
            require_media(request, raw, MULTIPART_MEDIA)
        except RequestRefused as e:
            return error(e.status, e.code, e.message)
        async with replayed(request, raw).form() as form:  # closes the spooled parts
            for part in form.getlist("file"):
                if not isinstance(part, UploadFile) or not part.filename:
                    return error(
                        422,
                        "validation",
                        "each file part carries its relative path as its filename",
                    )
                data = await part.read()
                try:
                    files[part.filename] = data.decode("utf-8")
                except UnicodeDecodeError:
                    binary[part.filename] = base64.b64encode(data).decode("ascii")
            body = {k: v for k in ("command_id", "by") if isinstance(v := form.get(k), str) and v}
        if not files and not binary:
            return error(422, "validation", "an upload carries at least one file part")
        args: dict[str, Any] = {"files": files}
        if binary:
            args["binary"] = binary
        try:
            command = parsed(owner, "UploadInputs", args, body, by)
        except wire.WireInvalid as e:
            return error(422, e.code, e.message)
        receipt = await commit(command)
        if isinstance(receipt, JSONResponse):
            return receipt

        def stored() -> list[dict[str, Any]]:
            with reading(owner) as view:
                return upload_documents(
                    [*files, *binary], documents_after(view, receipt.seq), known_documents(view)
                )

        documents = await run_in_threadpool(stored)
        return JSONResponse({"receipt": wire.receipt_to_json(receipt), "documents": documents})

    return router


class _KeepBy:
    """The default :class:`ByBinder`: the body's ``by`` is kept (a trusted caller)."""

    def bind(self, command: named.NamedCommand) -> named.NamedCommand:
        return command


def _keep_by() -> ByBinder:
    return _KeepBy()


def _invalid_as_422[**P](fn: Callable[P, Any]) -> Callable[P, Any]:
    """A read endpoint that answers 422 ``validation`` for a malformed ref, not 500."""

    @functools.wraps(fn)
    def endpoint(*args: P.args, **kwargs: P.kwargs) -> Any:
        try:
            return fn(*args, **kwargs)
        except wire.WireInvalid as e:
            return error(422, e.code, e.message)

    return endpoint
