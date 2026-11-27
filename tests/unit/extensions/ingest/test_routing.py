"""Group 10: the three setups (no System 1 model sends everything to the general model)."""

from __future__ import annotations

import pytest

from aksha.extensions.ingest.profiles import PROFILES
from aksha.extensions.ingest.routing import (
    NATIVE_FILES_OFF,
    NO_VISION,
    OVER_INLINE,
    resolve_bindings,
    resolve_route,
    setup_of,
)
from aksha.extensions.ingest.types import Document, FileInput, Route
from aksha.uploads import UploadIngester
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


# --- native file input (ADR-P07 G6): off by default, recorded either way ---------------

PDF_INPUT = FileInput(frozenset({"application/pdf"}), 1_000_000)


def _pdf_doc(size: int = 2_000) -> Document:
    return Document("D-1", 1, "sha256:" + "0" * 64, "SYN-terms.pdf", "upload", None, None,
                    "application/pdf", None, None, size)  # fmt: skip


def test_a_pdf_is_text_with_the_reason_while_native_files_are_off() -> None:
    assert resolve_route(_pdf_doc(), None) == Route("text")  # the model declares no files
    assert resolve_route(_pdf_doc(), None, files=PDF_INPUT) == Route("text", None, NATIVE_FILES_OFF)
    on = FileInput(PDF_INPUT.accepts, PDF_INPUT.max_inline_bytes, enabled=True)
    assert resolve_route(_pdf_doc(), None, files=on) == Route("native_file")
    assert resolve_route(_pdf_doc(2_000_000), None, files=on) == Route("text", None, OVER_INLINE)
    docx = Document("D-2", 1, "sha256:" + "0" * 64, "a.docx", "upload", None, None,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    None, None, 10)  # fmt: skip
    assert resolve_route(docx, None, files=on) == Route("text")


@pytest.mark.parametrize(("enabled", "kind"), [(False, "text"), (True, "native_file")])
def test_an_uploaded_pdf_records_the_route_taken(enabled: bool, kind: str) -> None:
    fx.need("pypdf")
    files = FileInput(PDF_INPUT.accepts, 10_000_000, enabled=enabled)
    served = UploadIngester(files=files).ingest(
        {"SYN-terms.pdf": fx.text_pdf(["Synthetic terms: renews yearly."])}, {}, 1, None
    )
    (stored,) = [p for e in served for t, p in e.facts if t == "document.stored"]
    assert stored["route"]["kind"] == kind
    assert (stored["route"].get("reason") == NATIVE_FILES_OFF) is not enabled
    # Either way the PDF is parsed and its text served: decide then extract always runs.
    assert b"renews yearly" in served[0].data
    (plain,) = [p for e in UploadIngester().ingest(
        {"SYN-terms.pdf": fx.text_pdf(["Synthetic terms."])}, {}, 1, None)
        for t, p in e.facts if t == "document.stored"]  # fmt: skip
    assert "route" not in plain  # a model with no native files records no route, as before
