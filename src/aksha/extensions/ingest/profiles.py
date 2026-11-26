"""Images per target model (design note 3.4): each provider's own resize rule, in code.

:data:`PROFILES` holds four image profiles, named for their arithmetic and read from the
providers' documentation on 26 Nov 2026. The dates matter: one provider's many-image
edge was 2,000 px in Feb 2026 and is 3,000 px in its documentation now, so it is
configuration (:func:`with_many_image_edge`), not a constant.

=====================  ======  =========  ========  =======================================
Profile                Patch   Long edge  Budget    Tokens
=====================  ======  =========  ========  =======================================
``patch28-standard``   28 px   1,568      1,568     ``ceil(w/28) * ceil(h/28)``
``patch28-hires``      28 px   2,576      4,784     the same, the provider's newer models
``patch32-high``       32 px   2,048      2,500     ``ceil(w/32) * ceil(h/32)`` times 1.2
``flat-1120``          none    3,072 [1]  none      a flat 1,120 per image
=====================  ======  =========  ========  =======================================

[1] The flat-cost provider documents no edge limit; 3,072 is a placeholder so a huge image
is never sent as it is.

Resize rules (``ImageProfile.fit``):

- ``patch_budget``: the largest aspect-preserving size whose sides, padded up to a
  multiple of 28, are at or under the long edge and whose patch count is at or under the
  budget, found by binary search over the long side, the short side rounded half to even.
  1920x1080 becomes 1456x819 (1,560 tokens); 4032x3024 becomes 1270x952 (1,564).
- ``patch_area``: fit the long side to the edge (the short side floored), then if the
  patches are over the budget scale by area to it and round the 32 px grid down.
  2048x2048 becomes 1600x1600, 2,500 patches, 3,000 tokens.
- ``edge``: only the long edge.

Nothing is ever enlarged. A PNG, JPEG or WebP the intake left untouched and that already
fits goes out byte for byte (``passed_through``); everything else is encoded once from the
canonical pixels (JPEG quality 88 for a photo source, PNG otherwise), never from an
earlier sent copy. :class:`SentCopyCache` keys sent copies by (source hash, profile, edge).
:func:`plan_request_images` counts images across a whole request against the profile's
many-image rule.
"""

from __future__ import annotations

import hashlib
import io
import math
from collections.abc import Sequence
from dataclasses import replace
from fractions import Fraction
from typing import Literal

from aksha.extensions.ingest.images import Frame
from aksha.extensions.ingest.types import ImagePlan, ImageProfile, SentImage

_PATCH28_ACCEPTS = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_REFS = frozenset({"file_id", "url", "base64"})

PROFILES: dict[str, ImageProfile] = {
    "patch28-standard": ImageProfile(
        "patch28-standard", _PATCH28_ACCEPTS, "patch", "patch_budget", 28, 1568, 1568, None,
        1_000_000, None, 20, 3000, 5_000_000, False, _REFS,
    ),
    "patch28-hires": ImageProfile(
        "patch28-hires", _PATCH28_ACCEPTS, "patch", "patch_budget", 28, 2576, 4784, None,
        1_000_000, None, 20, 3000, 5_000_000, False, _REFS,
    ),
    "patch32-high": ImageProfile(
        "patch32-high", frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"}),
        "patch", "patch_area", 32, 2048, 2500, 30_000, 1_200_000, None, None, None, 20_000_000,
        True, _REFS,
    ),
    "flat-1120": ImageProfile(
        "flat-1120",
        frozenset({"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}),
        "flat", "edge", None, 3072, None, None, 1_000_000, 1120, None, None, 7_000_000, True,
        frozenset({"uri", "base64"}),
    ),
}  # fmt: skip
"""The four profiles of the first build, keyed by profile ID (module doc)."""

FORMULAS = {"patch_budget": "patch_budget_v1", "patch_area": "patch_area_v1", "edge": "flat_v1"}


def _padded(n: int, patch: int) -> int:
    return -(-n // patch) * patch


def patches(profile: ImageProfile, w: int, h: int) -> int:
    """The patch count of a ``w`` by ``h`` image (0 for a flat-cost profile)."""
    if profile.patch_px is None:
        return 0
    p = profile.patch_px
    return (-(-w // p)) * (-(-h // p))


def estimate_tokens(profile: ImageProfile, w: int, h: int) -> int:
    """Image tokens from the profile's formula, never a flat guess."""
    if profile.cost == "flat":
        return int(profile.flat_tokens or 0)
    return (patches(profile, w, h) * profile.token_multiplier_ppm + 999_999) // 1_000_000


def _budget_fits(profile: ImageProfile, w: int, h: int, edge: int) -> bool:
    p = int(profile.patch_px or 28)
    within = _padded(w, p) <= edge and _padded(h, p) <= edge
    return within and (profile.max_patches is None or patches(profile, w, h) <= profile.max_patches)


def fit_size(profile: ImageProfile, w: int, h: int, max_edge: int | None = None) -> tuple[int, int]:
    """The size the provider would send ``w`` by ``h`` at (module doc, resize rules).

    ``max_edge`` lowers the profile's long edge (the many-image rule); it never raises it.
    """
    edge = min(profile.max_long_edge, max_edge or profile.max_long_edge)
    if profile.fit == "patch_budget":
        if _budget_fits(profile, w, h, edge):
            return w, h
        long0, short0 = (w, h) if w >= h else (h, w)
        lo, hi, best = 1, long0, (1, 1)
        while lo <= hi:
            mid = (lo + hi) // 2
            short = max(1, round(Fraction(mid * short0, long0)))
            size = (mid, short) if w >= h else (short, mid)
            if _budget_fits(profile, size[0], size[1], edge):
                best, lo = size, mid + 1
            else:
                hi = mid - 1
        return best
    if max(w, h) > edge:
        if w >= h:
            w, h = edge, max(1, h * edge // w)
        else:
            w, h = max(1, w * edge // h), edge
    if profile.fit == "patch_area" and profile.max_patches is not None:
        p = int(profile.patch_px or 32)
        if patches(profile, w, h) > profile.max_patches:
            sf = math.sqrt(p * p * profile.max_patches / (w * h))
            ws, hs = w * sf / p, h * sf / p
            sf *= min(math.floor(ws + 1e-9) / ws, math.floor(hs + 1e-9) / hs)
            w, h = max(1, int(w * sf + 1e-9)), max(1, int(h * sf + 1e-9))
    return w, h


class SentCopyCache:
    """Sent copies by (source hash, profile ID, many-image edge); in memory, re-derivable."""

    def __init__(self) -> None:
        """An empty cache."""
        self._held: dict[tuple[str, str, int | None], SentImage] = {}
        self.encodes = 0

    def get(self, key: tuple[str, str, int | None]) -> SentImage | None:
        """The cached copy, or None."""
        return self._held.get(key)

    def put(self, key: tuple[str, str, int | None], sent: SentImage) -> None:
        """Keep ``sent`` under ``key``."""
        self._held[key] = sent


def _encode(pixels: object, fmt: Literal["JPEG", "PNG"], quality: int) -> bytes:
    from PIL import Image

    assert isinstance(pixels, Image.Image)
    buf = io.BytesIO()
    if fmt == "JPEG":
        im = pixels
        if im.mode in ("RGBA", "LA", "P"):
            base = Image.new("RGB", im.size, (255, 255, 255))
            base.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = base
        elif im.mode != "RGB" and im.mode != "L":
            im = im.convert("RGB")
        im.save(buf, "JPEG", quality=quality)
    else:
        pixels.save(buf, "PNG")
    return buf.getvalue()


def prepare_image(
    source_sha256: str,
    frame: Frame,
    profile: ImageProfile,
    *,
    max_edge: int | None = None,
    cache: SentCopyCache | None = None,
) -> SentImage:
    """The sent copy of ``frame`` for ``profile`` (module doc); cached when ``cache`` is given."""
    key = (source_sha256, profile.profile_id, max_edge)
    if cache is not None:
        held = cache.get(key)
        if held is not None:
            return held
    pixels = frame.pixels
    sw, sh = pixels.width, pixels.height
    tw, th = fit_size(profile, sw, sh, max_edge)
    media = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
    passthrough = frame.passthrough
    if (
        passthrough is not None
        and (tw, th) == (sw, sh)
        and media.get(frame.source_format) in profile.accepts
        and len(passthrough) <= profile.max_bytes_per_image
    ):
        data, media_type, through = passthrough, media[frame.source_format], True
    else:
        photo = frame.source_format in ("JPEG", "MPO", "HEIF", "HEIC", "AVIF")
        fmt: Literal["JPEG", "PNG"] = "JPEG" if photo else "PNG"
        resized = pixels if (tw, th) == (sw, sh) else pixels.resize((tw, th), resample=3)
        data = _encode(resized, fmt, 88)
        quality = 88
        while len(data) > profile.max_bytes_per_image and (tw > 64 and th > 64):
            if fmt == "JPEG" and quality > 70:
                quality -= 9
            else:
                tw, th = max(1, tw * 4 // 5), max(1, th * 4 // 5)
                resized = pixels.resize((tw, th), resample=3)
            data = _encode(resized, fmt, quality)
        media_type, through = ("image/jpeg" if fmt == "JPEG" else "image/png"), False
        if cache is not None:
            cache.encodes += 1
    sent = SentImage(
        source_sha256=source_sha256,
        profile_id=profile.profile_id,
        source_wh=(sw, sh),
        sent_wh=(tw, th),
        scale_ppm=tw * 1_000_000 // max(1, sw),
        media_type=media_type,
        passed_through=through,
        blob="sha256:" + hashlib.sha256(data).hexdigest(),
        est_tokens=estimate_tokens(profile, tw, th),
        formula=FORMULAS[profile.fit],
        data=data,
    )
    if cache is not None:
        cache.put(key, sent)
    return sent


def plan_request_images(
    images: Sequence[SentImage],
    profile: ImageProfile,
    *,
    strategy: Literal["rederive", "ref_older"] = "rederive",
) -> tuple[ImagePlan, ...]:
    """What each image block of one request does, oldest first, counted across the request.

    At or under the profile's many-image threshold every image is sent as it is. Over it,
    ``rederive`` re-derives every image whose long side is over the many-image edge to
    that edge (from the original, through :func:`prepare_image`), so none goes out over
    the stricter limit; ``ref_older`` keeps the newest images up to the threshold and
    renders the older ones as their sidecar plus a reference, so the stricter limit never
    applies.
    """
    count = len(images)
    threshold = profile.many_image_threshold
    if threshold is None or count <= threshold:
        return tuple(ImagePlan(i, "send", None) for i in range(count))
    why = f"image not resent: {count} images in this request"
    if strategy == "ref_older":
        cut = count - threshold
        return tuple(
            ImagePlan(i, "ref_only", None, why) if i < cut else ImagePlan(i, "send", None)
            for i in range(count)
        )
    edge = profile.many_image_max_edge
    out: list[ImagePlan] = []
    for i, image in enumerate(images):
        if edge is not None and max(image.sent_wh) > edge:
            out.append(ImagePlan(i, "rederive", edge, f"over {edge} px with {count} images"))
        else:
            out.append(ImagePlan(i, "send", None))
    return tuple(out)


def with_many_image_edge(profile: ImageProfile, edge: int, as_of: str) -> ImageProfile:
    """``profile`` with its many-image edge read from configuration on ``as_of``."""
    return replace(profile, many_image_max_edge=edge, as_of=as_of)
