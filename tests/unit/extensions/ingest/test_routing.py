"""Group 10: the three setups (no System 1 model sends everything to the general model)."""

from __future__ import annotations

from aksha.extensions.ingest.profiles import PROFILES
from aksha.extensions.ingest.routing import (
    NO_VISION,
    resolve_bindings,
    resolve_route,
    setup_of,
)
from aksha.extensions.ingest.types import Document
from tests.support import ingest_fixtures as fx


def test_setup_1_no_system_1_models_everything_to_the_general_model_no_routing() -> None:
    assert setup_of(None, None) == "s2_only"
    decide, extract, generate = resolve_bindings("generate")
    for b in (decide, extract, generate):
        assert b.alias == "generate" and not b.routed
    assert decide.adapted and extract.adapted and not generate.adapted
    assert (decide.model_class, extract.model_class) == ("classification", "extraction")


def test_setup_2_a_decision_model_and_the_general_model_extracting_adapted() -> None:
    assert setup_of("doc_kind", None) == "s1_decide"
    decide, extract, generate = resolve_bindings("generate", decide="doc_kind")
    assert (decide.alias, decide.adapted, decide.routed) == ("doc_kind", False, True)
    assert (extract.alias, extract.adapted) == ("generate", True)
    assert generate.alias == "generate"


def test_setup_3_system_1_models_for_both() -> None:
    assert setup_of("doc_kind", "page_facts") == "s1_decide_extract"
    decide, extract, _ = resolve_bindings("generate", decide="doc_kind", extract="page_facts")
    assert (decide.alias, decide.adapted) == ("doc_kind", False)
    assert (extract.alias, extract.adapted) == ("page_facts", False)


def test_an_extraction_model_without_a_decision_model() -> None:
    assert setup_of(None, "page_facts") == "s1_extract"
    decide, extract, _ = resolve_bindings("generate", extract="page_facts")
    assert decide.adapted and not extract.adapted


def _doc(kind: str) -> Document:
    return Document("D-0001", 1, "sha256:" + "0" * 64, "x", "upload", None, None, kind, None,
                    None, 1)  # fmt: skip


def test_an_image_goes_native_to_a_vision_model_and_is_a_gap_without_one() -> None:
    fx.need("PIL")
    native = fx.run("a.png", fx.png_bytes(), profile=PROFILES["patch32-high"])
    assert native.route is not None and native.route.kind == "native_image"
    blind = fx.run("a.png", fx.png_bytes(), profile=None)
    assert blind.route is not None and blind.route.kind == "header_only"
    assert blind.route.reason == NO_VISION
    assert f"not shown: {NO_VISION}; only this header is in context" in blind.header


def test_every_other_type_is_its_text_representation_in_every_setup() -> None:
    for profile in (None, PROFILES["patch28-standard"]):
        assert resolve_route(_doc("application/pdf"), profile).kind == "text"
