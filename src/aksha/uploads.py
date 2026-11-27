"""The upload ingester the composition root wires in: the ingest extension behind the port.

:class:`UploadIngester` implements ``runtime.inputs.ports.DocumentIngester`` over
:func:`aksha.extensions.ingest.ingest.ingest`. For each uploaded path:

1. Its doc key: the path's known key when it was uploaded before (its version moves only
   when the bytes moved), else the next new one. The input path plays the part of the doc
   key a client passes to replace a document (ADR D2): ``UploadInputs`` replaces by path.
2. Intake runs, its parsers in worker processes (:mod:`aksha.extensions.ingest.worker`);
   the original bytes, and an image's sent copy, go to the blob store.
3. What the workspace serves at the path: a UTF-8 text file an agent's own tools parse
   (plain text, Markdown, CSV, JSON, HTML, SVG) as it is; anything converted (PDF, Office,
   email, images, text in another charset, a type conflict) as its header, a blank line
   and its representation; a gap or a refusal as its header alone, so the model is told.
4. Each attachment is served at ``<path>.parts/<n>-<name>``, ingested the same way. An
   upload that also names one of those paths itself is refused (``ValueError``).
5. The facts, only for a path whose bytes moved: ``document.stored``, then
   ``document.gap`` or ``document.refused``.

``profile`` is the image profile of the model that reads the inputs (``AKSHA_IMAGE_PROFILE``),
None when no vision-capable model is configured: an image is then the header alone with
``not shown: no vision-capable model configured``. The native image call itself lands with
the agent loop's provider adapters; the sent copy is prepared, stored and recorded now.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any

from aksha.extensions.ingest import events
from aksha.extensions.ingest.header import DATA_RULE, escape_name
from aksha.extensions.ingest.ingest import Ingested, ingest
from aksha.extensions.ingest.parse import Parse
from aksha.extensions.ingest.profiles import PROFILES, SentCopyCache
from aksha.extensions.ingest.types import Caps, FileInput, ImageProfile, Refusal
from aksha.extensions.ingest.worker import ParserPool
from aksha.runtime.inputs.types import (
    DOC_PREFIX,
    DOCUMENT_GAP,
    DOCUMENT_REFUSED,
    DOCUMENT_STORED,
    IngestedInput,
    KnownDocument,
)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def child_path(parent: str, n: int, name: str) -> str:
    """Where attachment ``n`` (from 1) of the input at ``parent`` is served."""
    safe = _SAFE.sub("_", name.rsplit("/", 1)[-1]).strip("._") or "attachment"
    return f"{parent}.parts/{n}-{safe[:80]}"


def profile_named(name: str | None) -> ImageProfile | None:
    """The image profile ``name`` (``AKSHA_IMAGE_PROFILE``), or None for none or ``none``."""
    if not name or name == "none":
        return None
    found = PROFILES.get(name)
    if found is None:
        raise ValueError(f"no image profile {name!r}; one of {sorted(PROFILES)} or none")
    return found


def _refused_text(r: Refusal) -> bytes:
    line = (f"[not stored] {escape_name(r.name_as_given)} (name as uploaded)\n"
            f"refused: {r.cap} {r.value:,}, over the limit {r.limit:,}; nothing was kept\n"
            f"{DATA_RULE}\n")  # fmt: skip
    return line.encode("utf-8")


class UploadIngester:
    """The ``DocumentIngester`` of the composition root (module doc)."""

    def __init__(
        self,
        *,
        caps: Caps | None = None,
        profile: ImageProfile | None = None,
        parse: Parse | None = None,
        files: FileInput | None = None,
    ) -> None:
        """Ingest with ``caps`` for a reader whose image profile is ``profile``.

        ``files`` is the reader's native file input and its opt-in (ADR-P07 G6); None when
        the bound model declares none, so no file route is recorded.

        ``parse`` runs the type check and the converters; the default is a
        :class:`~aksha.extensions.ingest.worker.ParserPool`, so every parser runs in a worker
        process under the caps' time, memory and CPU limits.
        """
        self.caps = caps or Caps()
        self.profile = profile
        self.files = files
        self.cache = SentCopyCache()
        self.parse: Parse = parse if parse is not None else ParserPool()

    def ingest(
        self,
        files: Mapping[str, bytes],
        known: Mapping[str, KnownDocument],
        first_key: int,
        put: Callable[[bytes], str] | None,
    ) -> tuple[IngestedInput, ...]:
        """One entry per path to serve, attachments included (module doc)."""
        counter = [first_key]
        out: list[IngestedInput] = []
        paths: dict[str, str] = {}
        fresh: dict[str, bool] = {}
        given: dict[str, bytes] = {}

        def key_for(parent: str, n: int, child: str, body: bytes) -> tuple[str, int]:
            where = child_path(paths[parent], n, child)
            k, v, m = key(where, body)
            paths[k], fresh[k], given[k] = where, m, body
            return k, v

        def key(path: str, data: bytes) -> tuple[str, int, bool]:
            held = known.get(path)
            blob = _sha(data)
            if held is None:
                k = f"{DOC_PREFIX}-{counter[0]:04d}"
                counter[0] += 1
                return k, 1, True
            if held.blob == blob:
                return held.doc_key, held.version, False
            return held.doc_key, held.version + 1, True

        for path in sorted(files):
            data = files[path]
            name = path.rsplit("/", 1)[-1]
            if len(data) > self.caps.max_bytes:
                r = Refusal("max_bytes", len(data), self.caps.max_bytes, name, "upload")
                fact = (DOCUMENT_REFUSED, events.refused(r, path))
                out.append(IngestedInput(path, _refused_text(r), (fact,)))
                continue
            doc_key, version, moved = key(path, data)
            paths[doc_key], fresh[doc_key], given[doc_key] = path, moved, data

            got = ingest(name, data, doc_key=doc_key, version=version, caps=self.caps,
                         key_for=key_for, put=put, profile=self.profile,
                         cache=self.cache, parse=self.parse, files=self.files)  # fmt: skip
            assert isinstance(got, Ingested)  # the byte cap ran above
            for item in got.walk():
                k = item.document.doc_key
                out.append(self._serve(item, paths[k], given[k], fresh[k], put))
        seen: set[str] = set()
        for entry in out:  # an uploaded path that is also an attachment's path: refuse both
            if entry.path in seen:
                raise ValueError(f"{entry.path!r} is both an uploaded file and an attachment")
            seen.add(entry.path)
        return tuple(out)

    def _serve(
        self,
        item: Ingested,
        path: str,
        original: bytes,
        moved: bool,
        put: Callable[[bytes], str] | None,
    ) -> IngestedInput:
        data = original if item.as_given else item.record_text.encode("utf-8")
        if item.route is not None and item.route.sent is not None and put is not None:
            put(item.route.sent.data)
        if not moved:
            return IngestedInput(path, data, ())
        facts: list[tuple[str, Mapping[str, Any]]] = [(DOCUMENT_STORED, events.stored(item, path))]
        if item.gap is not None:
            facts.append((DOCUMENT_GAP, events.gap(item, path)))
        for r in item.refusals:
            facts.append((DOCUMENT_REFUSED, events.refused(r, path)))
        return IngestedInput(path, data, tuple(facts))
