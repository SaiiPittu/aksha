"""Images at intake (design note 3.1, ``image_decode``): decoded, oriented, sRGB, stripped.

Pillow (MIT-CMU) comes with ``aksha-harness[ingest]``. HEIC and HEIF decode only when
``pillow-heif`` (1.9.0 or later, never the discontinued ``pi-heif``) is installed
separately: its binary wheels bundle libheif and libde265 (LGPL-3.0) and an x265 encoder
(GPL-2.0), and say the wheel as a whole is GPL-2.0, so no extra of this package installs
it. Without it a HEIC file is a gap ``unsupported`` that says so.

In order, all code:

1. The header's width times height is checked against ``Caps.max_pixels`` before any
   pixel is decoded; Pillow's own ``MAX_IMAGE_PIXELS`` guard stays on, and its warning is
   turned into a refusal. Over either is ``refused`` (``decompression_cap``).
2. ``ImageOps.exif_transpose`` applies the EXIF orientation, so a phone photo is upright.
3. An embedded ICC profile is converted to sRGB; the mode becomes RGB, RGBA or L.
4. Metadata is never carried over: a re-encode writes fresh bytes with no EXIF (GPS
   included), XMP, ICC or text chunks.
5. An animated GIF keeps frame 0; the other frames are counted in ``dropped``. A
   multi-page TIFF is split into pages, each its own ``p.N`` anchor in the sidecar.

The original bytes are never changed: intake returns the canonical pixels of each frame
and, for a PNG, JPEG or WebP that needed no change at all (no orientation, no metadata,
one frame), the original bytes for pass-through. The sidecar (the representation's text)
holds only what code knows and says the image is not described, because no OCR or vision
rung is declared in this build.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aksha.extensions.ingest import sniff
from aksha.extensions.ingest.types import (
    Anchor,
    Caps,
    Converted,
    Gap,
    ImageInfo,
    PageRung,
    Representation,
)

if TYPE_CHECKING:
    from PIL.Image import Image

EXTRA = "aksha-harness[ingest]"
NOT_DESCRIBED = "description: not described (no OCR or vision description configured)"
PASS_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})


@dataclass(frozen=True)
class Frame:
    """One canonical frame: upright sRGB pixels, and the original bytes when untouched."""

    pixels: Image
    source_format: str
    passthrough: bytes | None


@dataclass(frozen=True)
class Decoded:
    """An image's canonical frames and facts; ``converted`` holds the sidecar."""

    frames: tuple[Frame, ...]
    converted: Converted


def heif_available() -> bool:
    """Whether a HEIC decoder (pillow-heif) is installed and registered."""
    try:
        import pillow_heif
    except ImportError:
        return False
    pillow_heif.register_heif_opener()
    return True


def _has_metadata(im: Any) -> bool:
    info = getattr(im, "info", {}) or {}
    if any(k in info for k in ("exif", "icc_profile", "xmp", "XML:com.adobe.xmp", "comment")):
        return True
    if getattr(im, "text", None):
        return True
    try:
        return len(im.getexif()) > 0
    except Exception:
        return True


def _to_srgb(frame: Any, icc: bytes | None) -> tuple[Any, bool]:
    """``frame`` in sRGB, and whether a profile conversion ran."""
    from PIL import ImageCms

    converted = False
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            dst = ImageCms.createProfile("sRGB")
            mode = "RGBA" if "A" in frame.getbands() else "RGB"
            frame = ImageCms.profileToProfile(frame.convert(mode), src, dst, outputMode=mode)
            converted = True
        except Exception:
            converted = False
    if frame.mode in ("RGB", "RGBA", "L"):
        return frame, converted
    has_alpha = "A" in frame.getbands() or "transparency" in frame.info
    return frame.convert("RGBA" if has_alpha else "RGB"), converted


def decode(data: bytes, detected_type: str, caps: Caps | None = None) -> Decoded:
    """The canonical frames and the sidecar of an image, or a gap (module doc)."""
    caps = caps or Caps()
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError:
        return Decoded((), Converted(gap=Gap("unsupported", f"image reading needs the {EXTRA} "
                                             "extra (Pillow)")))  # fmt: skip
    if detected_type in (sniff.HEIC, sniff.HEIF) and not heif_available():
        detail = ("HEIC decoding needs pillow-heif, installed separately (its binary wheels "
                  "are GPL-2.0, so no extra installs it)")  # fmt: skip
        return Decoded((), Converted(gap=Gap("unsupported", detail)))
    steps = ("image_decode",)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data))
            w, h = im.size
            if w * h > caps.max_pixels:
                detail = f"decompression_cap: {w}x{h} pixels, over {caps.max_pixels}"
                return Decoded((), Converted(gap=Gap("refused", detail, steps)))
            fmt = str(im.format or "")
            count = int(getattr(im, "n_frames", 1) or 1)
            animated = fmt == "GIF" and count > 1
            keep = 1 if animated or fmt != "TIFF" else min(count, caps.max_pages)
            frames: list[Frame] = []
            facts: list[tuple[int, int, bool]] = []
            stripped = False
            for i in range(keep):
                im.seek(i)
                frame = im.copy()
                frame.load()
                meta = _has_metadata(im)
                orientation = int(im.getexif().get(0x0112, 1) or 1)
                upright = ImageOps.exif_transpose(frame) if orientation != 1 else frame
                upright, _ = _to_srgb(upright, im.info.get("icc_profile"))
                untouched = fmt in PASS_FORMATS and count == 1 and orientation == 1 and not meta
                stripped = stripped or meta
                frames.append(Frame(upright, fmt, data if untouched else None))
                facts.append((upright.width, upright.height, orientation != 1))
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        return Decoded((), Converted(gap=Gap("refused", f"decompression_cap: {e}", steps)))
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as e:
        return Decoded((), Converted(gap=Gap("unreadable", f"the image does not decode: "
                                             f"{type(e).__name__}", steps)))  # fmt: skip
    label = sniff.LABELS.get(detected_type, fmt)
    dropped: list[str] = []
    if animated:
        dropped.append(f"{count - 1} animation frames after the first (frame 0 kept)")
    if fmt == "TIFF" and count > keep:
        dropped.append(f"TIFF pages {keep + 1}-{count}: over the {caps.max_pages}-page cap")
    oriented = any(f[2] for f in facts)
    parts: list[str] = []
    anchors: list[Anchor] = []
    pages: list[PageRung] = []
    if len(frames) > 1:
        at = 0
        for n, (fw, fh, rot) in enumerate(facts, start=1):
            head = f"[p.{n}]\n"
            body = f"{label} page {n}, {fw}x{fh}{'; EXIF rotation applied' if rot else ''}\n"
            body += NOT_DESCRIBED + "\n\n"
            parts.append(head + body)
            anchors.append(Anchor(f"p.{n}", at + len(head), at + len(head) + len(body) - 2))
            pages.append(PageRung(f"p.{n}", "parse_fast", "extracted"))
            at += len(head) + len(body)
    else:
        fw, fh, rot = facts[0]
        line = f"{label}, {fw}x{fh}{' after EXIF orientation' if rot else ''}; 1 frame kept"
        parts.append(line + "\n" + NOT_DESCRIBED + "\n")
        pages.append(PageRung("image", "parse_fast", "extracted"))
    w0, h0 = facts[0][0], facts[0][1]
    info = ImageInfo(label, (w0, h0), len(frames), oriented, stripped,
                     frames_dropped=count - 1 if animated else 0)  # fmt: skip
    from PIL import __version__ as pil_version

    rep = Representation("".join(parts), tuple(anchors), (f"Pillow {pil_version}",),
                         tuple(pages), tuple(dropped), page_count=len(frames), image=info,
                         described=False)  # fmt: skip
    return Decoded(tuple(frames), Converted(representation=rep))
