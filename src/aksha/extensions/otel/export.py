"""Export a run's trace with the OpenTelemetry SDK, one OTLP JSON Lines file per segment.

``export_run`` converts each :class:`~aksha.extensions.otel.trace.SpanRecord` into the
SDK's own ``ReadableSpan`` (recorded times, derived IDs) and writes it through the
``opentelemetry-exporter-otlp-json-file`` exporter to ``<out>/<run>/<segment>.otlp.jsonl``.
The file is replaced on every export, so a re-export of a run overwrites it (D6, D8).
The resource is built with ``Resource({...})``, never ``Resource.create``, which would add
a random instance ID; ``service.instance.id`` is a hash of the store ID, so two exports
of a fake-mode run are byte-identical. Without the extra installed, :class:`ExporterMissing`
names the install command.
"""

from __future__ import annotations

import hashlib
import io
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from aksha.extensions.otel import semconv as sc
from aksha.extensions.otel.trace import Segment, SpanRecord, build_trace
from aksha.runtime.api import Event

INSTALL_HINT = "pip install 'aksha-harness[otel]'"


class ExporterMissing(RuntimeError):
    """The OpenTelemetry SDK or its OTLP JSON file exporter is not installed."""


def _sdk() -> Any:
    try:
        from opentelemetry import trace as api
        from opentelemetry.exporter.otlp.json.file import FileSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import Event as SdkEvent
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    except ImportError as e:  # the optional extra is missing
        raise ExporterMissing(f"trace export needs the otel extra: {INSTALL_HINT}") from e
    return {
        "api": api, "FileSpanExporter": FileSpanExporter, "Resource": Resource,
        "Event": SdkEvent, "ReadableSpan": ReadableSpan, "Scope": InstrumentationScope,
    }  # fmt: skip


def _readable(sdk: Mapping[str, Any], span: SpanRecord, resource: Any, scope: Any) -> Any:
    api = sdk["api"]
    sampled = api.TraceFlags(api.TraceFlags.SAMPLED)
    context = api.SpanContext(int(span.trace_id, 16), int(span.span_id, 16), False, sampled)
    parent = None
    if span.parent_id is not None:
        parent = api.SpanContext(int(span.trace_id, 16), int(span.parent_id, 16), False, sampled)
    links = [
        api.Link(api.SpanContext(int(link.trace_id, 16), int(link.span_id, 16), False, sampled),
                 dict(link.attributes))
        for link in span.links
    ]  # fmt: skip
    events = [sdk["Event"](e.name, dict(e.attributes), e.at_ns) for e in span.events]
    code = {"OK": api.StatusCode.OK, "ERROR": api.StatusCode.ERROR}.get(
        span.status, api.StatusCode.UNSET
    )
    return sdk["ReadableSpan"](
        name=span.name, context=context, parent=parent, resource=resource,
        attributes=dict(span.attributes), events=events, links=links,
        kind=api.SpanKind.INTERNAL, status=api.Status(code), start_time=span.start_ns,
        end_time=span.end_ns, instrumentation_scope=scope,
    )  # fmt: skip


def write_segments(
    segments: Sequence[Segment], out_dir: Path, *, store_id: str
) -> tuple[Path, ...]:
    """Write each segment's spans to ``out_dir/<run>/<segment>.otlp.jsonl``; their paths."""
    sdk = _sdk()
    instance = hashlib.sha256(f"aksha|{store_id}".encode()).hexdigest()[:16]
    resource = sdk["Resource"]({
        sc.SERVICE_NAME: "aksha", sc.SERVICE_INSTANCE: instance, sc.AKSHA_SEMCONV: sc.SEMCONV,
    })  # fmt: skip
    scope = sdk["Scope"](sc.INSTRUMENTATION_SCOPE, "0.1.0")
    paths: list[Path] = []
    for segment in segments:
        buffer = io.StringIO()
        exporter = sdk["FileSpanExporter"](stream=buffer)
        exporter.export([_readable(sdk, s, resource, scope) for s in segment.spans])
        exporter.shutdown()
        path = out_dir / segment.run / f"{segment.index}.otlp.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(buffer.getvalue(), encoding="utf-8")
        paths.append(path)
    return tuple(paths)


def export_run(
    events: Sequence[Event], run: str, out_dir: Path, *, store_id: str,
    capture: str | None = None,
) -> tuple[Path, ...]:  # fmt: skip
    """Build ``run``'s trace from its events and write one file per segment (module doc).

    ``capture`` defaults to the standard switch ``OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT``,
    and to ``NO_CONTENT`` when it is unset or unknown (D5).
    """
    mode = sc.capture_mode(capture if capture is not None else os.environ.get(sc.CAPTURE_ENV))
    segments = build_trace(events, run, store_id=store_id, capture=mode)
    return write_segments(segments, out_dir, store_id=store_id)
