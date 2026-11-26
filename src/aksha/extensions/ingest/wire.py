"""The parser worker's wire format: JSON frames with raw byte blobs, checked on the way in.

A frame is an 8-byte big-endian length, that many bytes of UTF-8 JSON (an object), then the
byte blobs the object's ``blobs`` list sizes, back to back. JSON, never pickle: the worker
parses hostile files, so what it sends back is untrusted, and decoding it here runs no code.
Every decoder checks each field's type and closed set (a gap reason, a rung, a trust) and
raises :class:`WireError` on anything else, which the pool records as ``parser_crashed``.

An image frame travels as its mode, size and raw pixel bytes (``Image.frombytes``), so no
image format is parsed outside the worker; a frame whose original bytes passed through
untouched says so, and the receiver uses the input bytes it already holds.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import fields
from typing import Any

from aksha.extensions.ingest import sniff
from aksha.extensions.ingest.images import Frame
from aksha.extensions.ingest.parse import Parsed
from aksha.extensions.ingest.types import (
    GAP_REASONS,
    Anchor,
    Caps,
    Child,
    Converted,
    Gap,
    ImageInfo,
    PageRung,
    Representation,
)

HEADER_BYTES = 8
MODES = ("RGB", "RGBA", "L")
RUNGS = ("parse_fast", "parse_layout", "parse_vision")
TRUSTS = ("extracted", "engine_written", "model_written")


class WireError(ValueError):
    """A frame that is not what the protocol allows."""


# --- frames -----------------------------------------------------------------------------


def frame(obj: dict[str, Any], blobs: Sequence[bytes] = ()) -> bytes:
    """One frame: ``obj`` with its ``blobs`` sizes, then the blobs."""
    body = json.dumps({**obj, "blobs": [len(b) for b in blobs]}, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")  # fmt: skip
    return b"".join([len(body).to_bytes(HEADER_BYTES, "big"), body, *blobs])


def header_length(raw: bytes) -> int:
    """The JSON length an 8-byte frame header gives."""
    return int.from_bytes(raw, "big")


def load(body: bytes) -> tuple[dict[str, Any], list[int]]:
    """A frame's JSON object and its blob sizes."""
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as e:
        raise WireError(f"not JSON: {type(e).__name__}") from None
    if not isinstance(obj, dict):
        raise WireError("a frame is an object")
    sizes = obj.get("blobs", [])
    if not isinstance(sizes, list) or not all(_is_int(n) and n >= 0 for n in sizes):
        raise WireError("blobs is a list of sizes")
    return obj, sizes


# --- checked readers --------------------------------------------------------------------


def _is_int(x: object) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _int(x: object) -> int:
    if not _is_int(x):
        raise WireError("expected an integer")
    return x  # type: ignore[return-value]


def _opt_int(x: object) -> int | None:
    return None if x is None else _int(x)


def _str(x: object) -> str:
    if not isinstance(x, str):
        raise WireError("expected a string")
    return x


def _opt_str(x: object) -> str | None:
    return None if x is None else _str(x)


def _bool(x: object) -> bool:
    if not isinstance(x, bool):
        raise WireError("expected a boolean")
    return x


def _list(x: object) -> list[Any]:
    if not isinstance(x, list):
        raise WireError("expected a list")
    return x


def _fixed(x: object, n: int) -> list[Any]:
    items = _list(x)
    if len(items) != n:
        raise WireError(f"expected a list of {n}")
    return items


def _dict(x: object) -> dict[str, Any]:
    if not isinstance(x, dict):
        raise WireError("expected an object")
    return x


def strs(x: object) -> tuple[str, ...]:
    """A checked list of strings."""
    return tuple(_str(s) for s in _list(x))


def _one_of(x: object, allowed: tuple[str, ...]) -> Any:
    if x not in allowed:
        raise WireError(f"not one of {allowed}")
    return x


# --- caps (host to worker, trusted) -----------------------------------------------------


def caps_json(caps: Caps) -> dict[str, Any]:
    """``caps`` as a JSON object."""
    return {f.name: getattr(caps, f.name) for f in fields(Caps)}


def caps_from(obj: dict[str, Any]) -> Caps:
    """The caps a job carries."""
    return Caps(**{f.name: obj[f.name] for f in fields(Caps) if f.name in obj})


# --- gaps, sniffed ----------------------------------------------------------------------


def gap_json(gap: Gap | None) -> dict[str, Any] | None:
    """A gap as JSON."""
    if gap is None:
        return None
    return {"reason": gap.reason, "detail": gap.detail, "steps": list(gap.steps)}


def gap_from(x: object) -> Gap | None:
    """A checked gap."""
    if x is None:
        return None
    d = _dict(x)
    return Gap(_one_of(d.get("reason"), GAP_REASONS), _str(d.get("detail")),
               strs(d.get("steps", [])))  # fmt: skip


def sniffed_json(found: sniff.Sniffed) -> dict[str, Any]:
    """A type check as JSON."""
    return {"detected_type": found.detected_type, "charset": found.charset, "text": found.text,
            "type_conflict": found.type_conflict, "gap": gap_json(found.gap)}  # fmt: skip


def sniffed_from(x: object) -> sniff.Sniffed:
    """A checked type check."""
    d = _dict(x)
    return sniff.Sniffed(_str(d.get("detected_type")), _opt_str(d.get("charset")),
                         _opt_str(d.get("text")), _opt_str(d.get("type_conflict")),
                         gap_from(d.get("gap")))  # fmt: skip


# --- representations --------------------------------------------------------------------


def _rep_json(rep: Representation) -> dict[str, Any]:
    image = rep.image
    return {
        "text": rep.text,
        "anchors": [[a.label, a.start, a.end] for a in rep.anchors],
        "converters": list(rep.converters),
        "pages": [[p.anchor, p.rung, p.trust] for p in rep.pages],
        "dropped": list(rep.dropped),
        "signals": [[k, v] for k, v in rep.signals],
        "page_count": rep.page_count,
        "image": None if image is None else {
            "format": image.format, "source_wh": list(image.source_wh), "frames": image.frames,
            "orientation_applied": image.orientation_applied,
            "metadata_stripped": image.metadata_stripped, "frames_dropped": image.frames_dropped,
        },
        "described": rep.described,
        "notes": list(rep.notes),
    }  # fmt: skip


def _pair(x: object) -> tuple[int, int]:
    a, b = _fixed(x, 2)
    return _int(a), _int(b)


def _rep_from(x: object) -> Representation:
    d = _dict(x)
    anchors = []
    for a in _list(d.get("anchors", [])):
        label, start, end = _fixed(a, 3)
        anchors.append(Anchor(_str(label), _int(start), _int(end)))
    pages = []
    for p in _list(d.get("pages", [])):
        anchor, rung, trust = _fixed(p, 3)
        pages.append(PageRung(_str(anchor), _one_of(rung, RUNGS), _one_of(trust, TRUSTS)))
    signals = []
    for s in _list(d.get("signals", [])):
        k, v = _fixed(s, 2)
        signals.append((_str(k), _int(v)))
    image = None
    if d.get("image") is not None:
        i = _dict(d["image"])
        image = ImageInfo(_str(i.get("format")), _pair(i.get("source_wh")), _int(i.get("frames")),
                          _bool(i.get("orientation_applied")),
                          _bool(i.get("metadata_stripped")),
                          _int(i.get("frames_dropped", 0)))  # fmt: skip
    return Representation(_str(d.get("text")), tuple(anchors), strs(d.get("converters", [])),
                          tuple(pages), strs(d.get("dropped", [])), tuple(signals),
                          _opt_int(d.get("page_count")), image, _bool(d.get("described", True)),
                          strs(d.get("notes", [])))  # fmt: skip


# --- a whole parse ----------------------------------------------------------------------


def parsed_frame(parsed: Parsed) -> bytes:
    """``parsed`` as one frame: children's bytes and frames' pixels as blobs."""
    blobs: list[bytes] = []

    def blob(data: bytes) -> int:
        blobs.append(data)
        return len(blobs) - 1

    conv = parsed.converted
    obj: dict[str, Any] = {
        "stage": "parsed",
        "sniffed": sniffed_json(parsed.found),
        "converted": None,
        "frames": [],
    }
    if conv is not None:
        obj["converted"] = {
            "representation": None if conv.representation is None
            else _rep_json(conv.representation),
            "gap": gap_json(conv.gap),
            "children": [{"name": c.name, "declared_type": c.declared_type,
                          "data": blob(c.data)} for c in conv.children],
        }  # fmt: skip
    for f in parsed.frames:
        px = f.pixels
        obj["frames"].append({"mode": px.mode, "size": [px.width, px.height],
                              "format": f.source_format,
                              "passthrough": f.passthrough is not None,
                              "pixels": blob(px.tobytes())})  # fmt: skip
    return frame(obj, blobs)


def parsed_from(obj: dict[str, Any], blobs: list[bytes], data: bytes, caps: Caps) -> Parsed:
    """A checked parse from a worker's reply; ``data`` is the input the host already holds."""

    def blob(i: object) -> bytes:
        n = _int(i)
        if not 0 <= n < len(blobs):
            raise WireError("a blob index out of range")
        return blobs[n]

    found = sniffed_from(obj.get("sniffed"))
    converted = None
    if obj.get("converted") is not None:
        c = _dict(obj["converted"])
        rep = None if c.get("representation") is None else _rep_from(c["representation"])
        children = tuple(Child(_str(k.get("name")), blob(k.get("data")),
                               _opt_str(k.get("declared_type")))
                         for k in map(_dict, _list(c.get("children", []))))  # fmt: skip
        converted = Converted(rep, gap_from(c.get("gap")), children)
    frames: list[Frame] = []
    budget = caps.max_pixels
    for f in map(_dict, _list(obj.get("frames", []))):
        mode = _one_of(f.get("mode"), MODES)
        w, h = _pair(f.get("size"))
        if w <= 0 or h <= 0 or w * h > budget:
            raise WireError("a frame over the pixel cap")
        budget -= w * h
        raw = blob(f.get("pixels"))
        if len(raw) != w * h * len(mode):
            raise WireError("a frame's pixels do not match its size")
        from PIL import Image  # the worker decoded it, so Pillow is installed

        pixels = Image.frombytes(mode, (w, h), raw)
        frames.append(Frame(pixels, _str(f.get("format")),
                            data if _bool(f.get("passthrough")) else None))  # fmt: skip
    return Parsed(found, converted, tuple(frames))
