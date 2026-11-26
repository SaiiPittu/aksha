"""Rendered reads: the artifacts a Task declares it reads, shown in its context (ADR-P08 D4, S1).

For each declared read, in declaration order, the current version of each slot it names
(``own``: the instance's key, or the unkeyed slot; ``all``: every slot of the kind, by key;
``from_field``: the keys listed in another read's field) is rendered once:

1. ``full``: its canonical JSON, when that is within ``read_cap_chars``.
2. ``summary``: over the cap, its kind's summary template filled from the content's
   top-level fields (``{field}``), or, for a kind with no template, its canonical JSON cut
   to the cap with head and tail kept. Either way the step records ``summary``.
3. ``ref_only``: once the read block would pass ``read_block_cap_chars``, only the ref.

Each rendering names the citation spans whose text it showed (S1): the spans the version
cites (``citation.bound`` with the version as carrier), all of them for ``full``, and for
``summary`` those whose JSON-escaped text is inside the rendered text. A ``full`` or
``summary`` rendering is a read of the version: it becomes a lineage input of the step.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.types import ArtifactKind, ArtifactVersion, InputRef
from aksha.runtime.artifacts.versions import current
from aksha.runtime.canonical import canonical_json
from aksha.runtime.context.types import ContextPolicy, Rendered, RenderMode
from aksha.runtime.grounding.spans import span_index
from aksha.runtime.grounding.types import SpanIndex
from aksha.runtime.projections.view import LogView
from aksha.runtime.tasks.keys import keys_of
from aksha.runtime.tasks.tree import CompiledTree
from aksha.runtime.tasks.types import FromField, InstanceRef, Read, artifact_keys
from aksha.runtime.tools.results import cap_text

_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class RenderedReads:
    """The read block's text, what each rendering showed, and the inputs it read."""

    text: str
    rendered: tuple[Rendered, ...]
    inputs: tuple[InputRef, ...]


def fill_template(template: str, content: Mapping[str, object]) -> str:
    """``template`` with each ``{field}`` replaced by that top-level field of ``content``.

    A string value is inserted as it is, any other value as canonical JSON; an unknown
    field is left as written. Pure and deterministic.
    """

    def one(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in content:
            return m.group(0)
        value = content[name]
        return value if isinstance(value, str) else canonical_json(value)

    return _FIELD.sub(one, template)


def declared_versions(
    view: LogView, tree: CompiledTree, instance: InstanceRef, *, blobs: BlobStore | None = None
) -> tuple[ArtifactVersion, ...]:
    """The current versions the instance's declared reads name, in render order, once each."""
    out: list[ArtifactVersion] = []
    seen: set[str] = set()
    for read in tree.reads(instance.task_path):
        for version in _versions(view, read, instance, blobs):
            if str(version.ref) not in seen:
                seen.add(str(version.ref))
                out.append(version)
    return tuple(out)


def _versions(
    view: LogView, read: Read, instance: InstanceRef, blobs: BlobStore | None
) -> list[ArtifactVersion]:
    if read.keys == "own":
        keys: tuple[str | None, ...] = (instance.key, None) if instance.key else (None,)
        for key in keys:
            found = current(view, read.kind, key, blobs=blobs)
            if found is not None:
                return [found]
        return []
    if isinstance(read.keys, FromField):
        listed = keys_of(view, artifact_keys(read.keys.kind, read.keys.path)) or ()
        found_keys: tuple[str | None, ...] = tuple(listed)
    else:
        found_keys = tuple(
            sorted(
                {e.payload.get("key") for e in view.events if _saved_kind(e.type, e.payload, read)},
                key=lambda k: "" if k is None else str(k),
            )
        )
    versions = (current(view, read.kind, k, blobs=blobs) for k in found_keys)
    return [v for v in versions if v is not None]


def _saved_kind(type_: str, payload: Mapping[str, object], read: Read) -> bool:
    return type_ == "artifact.saved" and payload.get("kind") == read.kind


def render_version(
    version: ArtifactVersion,
    kind: ArtifactKind | None,
    policy: ContextPolicy,
    index: SpanIndex,
    cited: Mapping[str, tuple[str, ...]],
) -> tuple[str, Rendered]:
    """One version's rendered text and what it showed (module doc, points 1 and 2)."""
    ref = str(version.ref)
    full = canonical_json(version.content)
    mode: RenderMode = "full"
    body = full
    if len(full) > policy.read_cap_chars:
        mode = "summary"
        if kind is not None and kind.summary:
            body, _ = cap_text(fill_template(kind.summary, version.content), policy.read_cap_chars)
        else:
            body, _ = cap_text(full, policy.read_cap_chars)
    text = f"[artifact {ref}, {mode}]\n{body}"
    shown: set[str] = set()
    for span_id in cited.get(ref, ()):
        span = index.get(span_id)
        if span is None:
            continue
        if mode == "full" or canonical_json(span.text)[1:-1] in body or span.text in body:
            shown.add(span_id)
    return text, Rendered(ref, mode, len(text), frozenset(shown))


def cited_spans(view: LogView) -> dict[str, tuple[str, ...]]:
    """The span IDs each artifact version cites, by carrier ref (``citation.bound``)."""
    out: dict[str, list[str]] = {}
    for e in view.events:
        if e.type == "citation.bound" and e.payload.get("form") == "span":
            out.setdefault(str(e.payload["carrier"]), []).append(str(e.payload["target"]))
    return {k: tuple(dict.fromkeys(v)) for k, v in out.items()}


def render_reads(
    view: LogView,
    tree: CompiledTree,
    instance: InstanceRef,
    policy: ContextPolicy,
    kinds: Mapping[str, ArtifactKind],
    *,
    blobs: BlobStore | None = None,
) -> RenderedReads:
    """The read block of ``instance``'s context (module doc). Pure."""
    versions = declared_versions(view, tree, instance, blobs=blobs)
    if not versions:
        return RenderedReads("", (), ())
    index = span_index(view)
    cited = cited_spans(view)
    parts: list[str] = []
    rendered: list[Rendered] = []
    inputs: list[InputRef] = []
    used = 0
    for version in versions:
        text, r = render_version(version, kinds.get(version.ref.kind), policy, index, cited)
        if used + len(text) > policy.read_block_cap_chars:
            text = f"[artifact {version.ref}, ref_only]"
            r = Rendered(str(version.ref), "ref_only", len(text))
        else:
            inputs.append(version.ref.as_input())
        used += len(text)
        parts.append(text)
        rendered.append(r)
    header = "Artifacts this Task reads (current versions):"
    return RenderedReads("\n\n".join((header, *parts)), tuple(rendered), tuple(inputs))
