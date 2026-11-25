from dataclasses import replace

import pytest

from aksha.runtime.events.catalogue import (
    CORE_EVENT_TYPES,
    PAYLOAD_CAP_BYTES,
    Catalogue,
    EventTypeSpec,
)
from aksha.runtime.events.errors import (
    BoundaryNotAllowed,
    CatalogueError,
    DedupKeyError,
    PayloadInvalid,
    PayloadTooLarge,
    UnknownBoundary,
    UnknownEventType,
)
from tests.support.events import MEMORY_RECALLED, PROBE_NOTED, TEST_TYPES


def test_core_catalogue_uses_the_proposed_names() -> None:
    names = {s.name for s in CORE_EVENT_TYPES}
    assert {"run.started", "run.ended", "artifact.saved", "recovery.completed"} <= names
    assert not names & {"walk.decided", "boot.completed", "instance.entered", "effect.intent"}
    Catalogue(CORE_EVENT_TYPES)  # the core types themselves pass every catalogue check


def test_catalogue_get_and_contains(catalogue: Catalogue) -> None:
    assert "probe.noted" in catalogue
    assert catalogue.get("probe.noted") is PROBE_NOTED
    assert "nope.never" not in catalogue
    with pytest.raises(UnknownEventType) as e:
        catalogue.get("nope.never")
    assert e.value.code == "unknown_event_type"
    assert set(catalogue.names()) >= {"probe.noted", "run.started"}


def test_catalogue_refuses_duplicates_bad_schemas_and_unknown_boundaries() -> None:
    with pytest.raises(CatalogueError, match="twice"):
        Catalogue((PROBE_NOTED, PROBE_NOTED))
    bad_schema = EventTypeSpec("probe.bad", {"type": 7}, "tests", frozenset({"U2"}))
    with pytest.raises(CatalogueError, match="schema"):
        Catalogue((bad_schema,))
    bad_boundary = EventTypeSpec("probe.x", {"type": "object"}, "tests", frozenset({"U99"}))
    with pytest.raises(UnknownBoundary):
        Catalogue((bad_boundary,))
    no_boundary = EventTypeSpec("probe.y", {"type": "object"}, "tests", frozenset())
    with pytest.raises(CatalogueError, match="boundary"):
        Catalogue((no_boundary,))
    unnamespaced = EventTypeSpec("probe", {"type": "object"}, "tests", frozenset({"U2"}))
    with pytest.raises(CatalogueError, match="namespace"):
        Catalogue((unnamespaced,))
    no_prefix = EventTypeSpec("probe.z", {"type": "object"}, "t", frozenset({"U2"}), "{run}")
    with pytest.raises(CatalogueError, match="prefix"):
        Catalogue((no_prefix,))


def test_catalogue_errors_are_value_errors() -> None:
    with pytest.raises(ValueError):
        Catalogue((PROBE_NOTED, PROBE_NOTED))


def test_validate_payload_names_the_failing_path(catalogue: Catalogue) -> None:
    catalogue.validate_payload("probe.noted", {"n": 1, "text": "ok"})
    with pytest.raises(PayloadInvalid) as e:
        catalogue.validate_payload("probe.noted", {"n": "one", "text": "ok"})
    assert e.value.code == "payload_invalid"
    assert e.value.path == "$.n"
    with pytest.raises(PayloadInvalid) as e:
        catalogue.validate_payload("probe.free", {"items": "x"})
    assert e.value.path == "$.items"
    with pytest.raises(PayloadInvalid) as e:
        catalogue.validate_payload("probe.noted", {"n": 1})
    assert e.value.path == "$"


def test_validate_payload_enforces_the_cap(catalogue: Catalogue) -> None:
    with pytest.raises(PayloadTooLarge) as e:
        catalogue.validate_payload("probe.free", {"blob": "x" * PAYLOAD_CAP_BYTES})
    assert e.value.code == "payload_too_large"
    assert e.value.size > PAYLOAD_CAP_BYTES


def test_check_boundary(catalogue: Catalogue) -> None:
    catalogue.check_boundary("stop.decided", "U3")
    with pytest.raises(BoundaryNotAllowed) as e:
        catalogue.check_boundary("stop.decided", "U2")
    assert e.value.code == "boundary_not_allowed"
    with pytest.raises(UnknownBoundary):
        catalogue.check_boundary("stop.decided", "U42")


def test_render_dedup_key_over_envelope_and_payload(catalogue: Catalogue) -> None:
    env = {"run": "R-0002", "owner": "o1", "session": None, "instance": None, "step": None}
    key = catalogue.render_dedup_key("stop.decided", env, {"walk": 7, "verdict": "stop"})
    assert key == "stop:R-0002/w007"
    assert catalogue.render_dedup_key("probe.free", env, {}) is None
    with pytest.raises(DedupKeyError) as e:
        catalogue.render_dedup_key("stop.decided", {**env, "run": None}, {"walk": 1})
    assert e.value.code == "dedup_key_field_missing"
    with pytest.raises(DedupKeyError) as e:
        catalogue.render_dedup_key("stop.decided", env, {})
    assert e.value.code == "dedup_key_field_missing"


def test_register_extension_adds_a_type() -> None:
    cat = Catalogue(TEST_TYPES)
    cat.register_extension(MEMORY_RECALLED, extension="memory")
    assert cat.get("memory.recalled") is MEMORY_RECALLED
    assert cat.is_extension("memory.recalled")
    assert not cat.is_extension("probe.noted")
    cat.register_extension(MEMORY_RECALLED, extension="memory")  # the same spec again: no-op
    changed = replace(MEMORY_RECALLED, schema_version=2)
    with pytest.raises(CatalogueError, match="already"):
        cat.register_extension(changed, extension="memory")
    assert cat.core_namespaces() == frozenset({"probe", "stop"})
    assert cat.core_dedup_prefixes() == frozenset({"probe:", "stop:"})


def test_register_extension_needs_the_extensions_namespace_and_writer() -> None:
    cat = Catalogue(TEST_TYPES)
    with pytest.raises(CatalogueError, match="named other"):
        cat.register_extension(replace(MEMORY_RECALLED, writer="other"), extension="other")
    with pytest.raises(CatalogueError, match="writer"):
        cat.register_extension(replace(MEMORY_RECALLED, writer="execution"), extension="memory")
    assert "memory.recalled" not in cat


def test_register_extension_refuses_core_names_and_prefixes() -> None:
    cat = Catalogue(TEST_TYPES)
    core_ns = EventTypeSpec("probe.sneaky", {"type": "object"}, "probe", frozenset({"U2"}))
    with pytest.raises(CatalogueError, match="core namespace"):
        cat.register_extension(core_ns, extension="probe")
    core_prefix = EventTypeSpec(
        "memory.kept", {"type": "object"}, "memory", frozenset({"U2"}), "stop:{run}"
    )
    with pytest.raises(CatalogueError, match="core dedup prefix"):
        cat.register_extension(core_prefix, extension="memory")
    assert "memory.kept" not in cat


def test_extension_refusal_is_pure() -> None:
    cat = Catalogue(TEST_TYPES)
    bad = replace(MEMORY_RECALLED, boundaries=frozenset({"U99"}))
    assert cat.extension_refusal(MEMORY_RECALLED, extension="memory") is None
    assert "U99" in str(cat.extension_refusal(bad, extension="memory"))
    assert "memory.recalled" not in cat


def test_payload_with_nul_is_refused_naming_the_path(catalogue: Catalogue) -> None:
    with pytest.raises(PayloadInvalid) as e:
        catalogue.validate_payload("probe.noted", {"n": 1, "text": "a\x00b"})
    assert e.value.path == "$.text" and "NUL" in str(e.value)
    with pytest.raises(PayloadInvalid) as e:
        catalogue.validate_payload("probe.noted", {"n": 1, "text": "t", "k\x00": 1})
    assert "NUL" in str(e.value)


def test_run_started_accepts_the_definition_fields(catalogue: Catalogue) -> None:
    from aksha.runtime.events.catalogue import RUN_STARTED

    payload = {
        "agent": "a", "cause": "request", "definition_hash": "sha256:" + "a" * 64,
        "extension_versions": {"echo": {"version": "0.1.0", "hash": "sha256:" + "b" * 64}},
        "api_version": "1.0", "runtime_commit": "abc", "dirty": False,
    }  # fmt: skip
    catalogue.validate_payload(RUN_STARTED.name, payload)
    with pytest.raises(PayloadInvalid):
        catalogue.validate_payload(RUN_STARTED.name, {**payload, "definition_hash": "md5:x"})


def test_core_catalogue_holds_the_step_facts_the_gate_records() -> None:
    from aksha.runtime.events.envelope import NewEvent

    core = Catalogue(CORE_EVENT_TYPES)
    assert {"step.started", "step.completed"} <= set(core.names())
    refused = {"outcome": "refused", "tool": "t", "code": "not_allowed", "layer": 2, "message": "m",
               "counted": True, "detail": []}  # fmt: skip
    core.validate_payload("step.completed", refused)
    with pytest.raises(PayloadInvalid):
        core.validate_payload("step.completed", {"outcome": "refused"})
    assert core.render_dedup_key("step.completed", NewEvent("x", {}, step="s1").context(), {}) == (
        "sdone:s1"
    )


def test_catalogue_copies_the_schema_it_registers() -> None:
    """Mutating the caller's nested schema after registration changes nothing held."""
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
        "additionalProperties": False,
    }
    spec = EventTypeSpec("probe.copied", schema, writer="t", boundaries=frozenset({"U2"}))
    held = Catalogue([spec])
    schema["properties"]["n"]["type"] = "string"  # type: ignore[index]
    schema["required"].append("x")  # type: ignore[attr-defined]
    assert held.validate_payload("probe.copied", {"n": 1}) == '{"n":1}'
    with pytest.raises(PayloadInvalid):
        held.validate_payload("probe.copied", {"n": "1"})
