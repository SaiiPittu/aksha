"""The ingest extension: every input stored, typed from content, converted and explained.

The library of the ingestion ADR (``docs/design/adr/ADR-ingestion-extension.md``), built
as an extension (decision D1): it imports only :mod:`aksha.runtime.api` and itself, and its
parsers are optional dependencies behind ``aksha-harness[ingest]`` (pypdf, python-docx,
python-pptx, openpyxl, Pillow). Without them each affected type is a gap that names the
extra; the core never needs them.

- ``sniff.py``: typing from bytes, charset, ZIP caps, the allow-list's types.
- ``text.py``, ``pdf.py``, ``office.py``, ``mail.py``: the fast-rung converters.
- ``images.py``: image intake; ``profiles.py``: per-model sizing, token estimates and the
  request-wide image count.
- ``routing.py``: bindings for the three setups and the route per consuming step.
- ``offload.py``: oversized tool results and user messages as documents.
- ``header.py``: what the model is told.
- ``ingest.py``: intake for one input; ``events.py``: the ``document.*`` payloads.

The composition root (``aksha.app``) wires it into uploads: ``UploadInputs`` and
``aksha run --input`` send each file's bytes, and the runtime calls the ingester through
its port (``runtime/inputs/ports.py``).
"""

from __future__ import annotations

from aksha.runtime.api import Extension

EXTENSION = Extension(name="ingest", version="0.1.0", requires_api=">=2.0,<3")
