"""Groups 7 to 9: image intake, each profile's resize formula, many images in one request."""

from __future__ import annotations

import io

import pytest

from aksha.extensions.ingest import images
from aksha.extensions.ingest.profiles import (
    PROFILES,
    SentCopyCache,
    estimate_tokens,
    fit_size,
    plan_request_images,
    prepare_image,
    with_many_image_edge,
)
from tests.support import ingest_fixtures as fx

# --- 7. Intake -----------------------------------------------------------------------------


def _open(data: bytes):  # type: ignore[no-untyped-def]
    from PIL import Image

    return Image.open(io.BytesIO(data))


def test_an_exif_rotated_jpeg_comes_out_upright_without_exif_or_gps() -> None:
    fx.need("PIL")
    data = fx.jpeg_rotated_with_gps(64, 32)
    raw = _open(data)
    assert raw.size == (64, 32) and raw.getexif()[0x0112] == 6
    assert raw.getexif().get_ifd(0x8825)  # the fixture carries a GPS position
    got = fx.run("IMG_0001.jpg", data, profile=PROFILES["patch28-standard"])
    rep = got.representation
    assert rep is not None and rep.image is not None
    assert rep.image.source_wh == (32, 64) and rep.image.orientation_applied
    assert rep.image.metadata_stripped
    assert got.route is not None and got.route.sent is not None
    sent = _open(got.route.sent.data)
    assert sent.size == (32, 64)
    assert len(sent.getexif()) == 0  # no orientation tag, no GPS
    assert "exif" not in sent.info
    # Upright: the red half (the image's left before rotation) is now on top.
    top, bottom = sent.convert("RGB").getpixel((16, 4)), sent.convert("RGB").getpixel((16, 60))
    assert isinstance(top, tuple) and isinstance(bottom, tuple)
    assert top[0] > 200 and bottom[2] > 200
    assert "EXIF rotation applied; metadata removed" in got.header


def test_an_animated_gif_keeps_frame_0_and_lists_the_rest() -> None:
    fx.need("PIL")
    got = fx.run("anim.gif", fx.animated_gif(4))
    rep = got.representation
    assert rep is not None and rep.image is not None
    assert len(got.frames) == 1 and rep.image.frames_dropped == 3
    assert rep.dropped == ("3 animation frames after the first (frame 0 kept)",)


def test_a_three_page_tiff_becomes_three_pages() -> None:
    fx.need("PIL")
    got = fx.run("scan.tiff", fx.multipage_tiff(3))
    rep = got.representation
    assert rep is not None
    assert len(got.frames) == 3 and rep.page_count == 3
    assert [a.label for a in rep.anchors] == ["p.1", "p.2", "p.3"]
    p2 = rep.anchor("p.2")
    assert p2 is not None and "page 2, 31x20" in rep.text[p2.start : p2.end]


def test_a_decompression_bomb_is_refused_before_decoding() -> None:
    fx.need("PIL")
    huge = fx.run("huge.png", fx.png_header_only(50_000, 50_000))
    assert huge.gap is not None and huge.gap.reason == "refused"
    assert huge.gap.detail.startswith("decompression_cap")
    over_ours = fx.run("big.png", fx.png_header_only(8_000, 6_000))
    assert over_ours.gap is not None
    assert over_ours.gap.detail == "decompression_cap: 8000x6000 pixels, over 40000000"


def test_heic_decodes_when_pillow_heif_is_present() -> None:
    fx.need("PIL")
    fx.need("pillow_heif")
    data = fx.heic_bytes()
    assert data is not None
    got = fx.run("IMG_2210.HEIC", data, profile=PROFILES["patch28-standard"])
    rep = got.representation
    assert rep is not None and rep.image is not None and rep.image.source_wh == (48, 32)
    assert got.route is not None and got.route.sent is not None
    assert got.route.sent.media_type == "image/jpeg"


def test_heic_without_pillow_heif_is_a_gap_that_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    fx.need("PIL")
    monkeypatch.setattr(images, "heif_available", lambda: False)
    heif = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\0" * 32
    got = fx.run("IMG.HEIC", heif)
    assert got.gap is not None and got.gap.reason == "unsupported"
    assert "pillow-heif" in got.gap.detail and "GPL-2.0" in got.gap.detail


def test_the_sidecar_says_what_code_knows_and_not_described() -> None:
    fx.need("PIL")
    rep = fx.run("a.png", fx.png_bytes(40, 30)).representation
    assert rep is not None and not rep.described
    assert rep.text == ("PNG image, 40x30; 1 frame kept\n"
                        "description: not described (no OCR or vision description "
                        "configured)\n")  # fmt: skip


# --- 8. Resize formulas, against the images research note's worked numbers ----------------


@pytest.mark.parametrize(
    ("profile", "size", "sent", "tokens"),
    [
        ("patch28-standard", (1920, 1080), (1456, 819), 1560),
        ("patch28-standard", (4032, 3024), (1270, 952), 1564),
        ("patch28-standard", (1170, 2532), (725, 1568), 1456),
        ("patch28-standard", (1000, 1000), (1000, 1000), 1296),
        ("patch28-hires", (4032, 3024), (2212, 1659), 4740),
        ("patch28-hires", (1920, 1080), (1920, 1080), 2691),
        ("patch32-high", (2048, 2048), (1600, 1600), 3000),
        ("patch32-high", (4032, 3024), (1824, 1368), 2942),
        ("patch32-high", (1920, 1080), (1920, 1080), 2448),
        ("patch32-high", (1000, 1000), (1000, 1000), 1229),
    ],
)
def test_each_provider_formula(
    profile: str, size: tuple[int, int], sent: tuple[int, int], tokens: int
) -> None:
    p = PROFILES[profile]
    assert fit_size(p, *size) == sent
    assert estimate_tokens(p, *sent) == tokens


def test_the_flat_profile_is_1120_whatever_the_size() -> None:
    p = PROFILES["flat-1120"]
    assert estimate_tokens(p, 1920, 1080) == 1120
    assert estimate_tokens(p, 64, 64) == 1120


def test_a_resized_copy_is_encoded_at_the_formula_size() -> None:
    fx.need("PIL")
    got = fx.run("shot.png", fx.sized_png(1920, 1080), profile=PROFILES["patch28-standard"])
    assert got.route is not None and got.route.sent is not None
    s = got.route.sent
    assert (s.sent_wh, s.est_tokens, s.passed_through) == ((1456, 819), 1560, False)
    assert _open(s.data).size == (1456, 819)
    assert s.scale_ppm == 1456 * 1_000_000 // 1920
    assert "sent: PNG 1456x819 for patch28-standard, about 1,560 image tokens" in got.header


def test_a_small_png_that_fits_is_passed_through_byte_for_byte() -> None:
    fx.need("PIL")
    data = fx.png_bytes(40, 30)
    got = fx.run("small.png", data, profile=PROFILES["patch28-standard"])
    assert got.route is not None and got.route.sent is not None
    assert got.route.sent.passed_through and got.route.sent.data == data
    assert "sent: the original as it was for patch28-standard" in got.header


def test_sent_copies_are_cached_by_hash_profile_and_edge() -> None:
    fx.need("PIL")
    decoded = images.decode(fx.sized_png(1920, 1080), "image/png")
    cache = SentCopyCache()
    profile = PROFILES["patch28-standard"]
    first = prepare_image("abc", decoded.frames[0], profile, cache=cache)
    again = prepare_image("abc", decoded.frames[0], profile, cache=cache)
    assert again is first and cache.encodes == 1
    other = prepare_image("abc", decoded.frames[0], PROFILES["patch32-high"], cache=cache)
    assert other.sent_wh == (1920, 1080) and other.passed_through  # it fits patch32-high
    hires = prepare_image("abc", decoded.frames[0], PROFILES["patch28-hires"], max_edge=1000,
                          cache=cache)  # fmt: skip
    assert max(hires.sent_wh) <= 1000 and cache.encodes == 2


# --- 9. Many images in one request ----------------------------------------------------------


def _sent(n: int, edge: int) -> list:  # type: ignore[type-arg]
    from aksha.extensions.ingest.types import SentImage

    return [SentImage(f"s{i}", "patch28-hires", (4000, 3000), (edge, edge * 3 // 4), 0,
                      "image/jpeg", False, f"sha256:{i:064x}", 4000, "patch_budget_v1")
            for i in range(n)]  # fmt: skip


def test_twenty_three_images_to_a_patch28_profile_none_over_the_limit() -> None:
    fx.need("PIL")
    profile = with_many_image_edge(PROFILES["patch28-hires"], 2000, "2026-02-04")
    decoded = images.decode(fx.sized_png(4032, 3024), "image/png")
    sent = [prepare_image(f"h{i}", decoded.frames[0], profile) for i in range(23)]
    assert all(max(s.sent_wh) == 2212 for s in sent)
    plan = plan_request_images(sent, profile)
    assert [p.action for p in plan] == ["rederive"] * 23
    redone = [prepare_image(f"h{p.index}", decoded.frames[0], profile, max_edge=p.max_edge)
              for p in plan]  # fmt: skip
    assert all(max(s.sent_wh) <= 2000 for s in redone)
    assert all(max(_open(s.data).size) <= 2000 for s in redone)


def test_at_the_threshold_every_image_is_sent_as_it_is() -> None:
    plan = plan_request_images(_sent(20, 2212), PROFILES["patch28-hires"])
    assert {p.action for p in plan} == {"send"}


def test_ref_older_keeps_the_newest_twenty_and_refers_to_the_rest() -> None:
    plan = plan_request_images(_sent(23, 2212), PROFILES["patch28-hires"], strategy="ref_older")
    assert [p.action for p in plan[:3]] == ["ref_only"] * 3
    assert {p.action for p in plan[3:]} == {"send"}
    assert plan[0].reason == "image not resent: 23 images in this request"


def test_the_current_3000_px_edge_leaves_patch28_sized_copies_alone() -> None:
    plan = plan_request_images(_sent(23, 2212), PROFILES["patch28-hires"])
    assert {p.action for p in plan} == {"send"}
