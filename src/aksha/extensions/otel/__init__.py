"""The trace exporter extension (ADR-P21 D8): a run's trace as OTLP JSON Lines files.

``semconv.py`` holds every OpenTelemetry name, pinned (D3, D4); ``trace.py`` builds the
trace as a pure projection of a run's events (D1, D2, D6); ``export.py`` hands the spans
to the official OpenTelemetry SDK and its OTLP JSON file exporter, imported lazily, from
the optional extra ``aksha[otel]``. Export runs after a segment ends, over a read-only
store, and never calls out: there is no network exporter here. The extension imports only
:mod:`aksha.runtime.api` and itself; it contributes nothing to agents.
"""

from __future__ import annotations

from aksha.runtime.api import Extension

EXTENSION = Extension(name="otel", version="0.1.0", requires_api=">=2.0,<3")
