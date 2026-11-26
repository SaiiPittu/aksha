from dataclasses import fields

from aksha.runtime.definitions.types import Agent, Extension, Projection
from tests.support.reducers import ProbeTrail


def test_agent_carries_the_amended_fields() -> None:
    names = [f.name for f in fields(Agent)]
    for amended in (
        "relations",
        "stop_policies",
        "model_aliases",
        "limits",
        "context_policy",
        "sources",
        "extensions",
        "requires_api",
    ):
        assert amended in names
    for old in ("edges", "directors", "roles"):
        assert old not in names


def test_agent_defaults() -> None:
    a = Agent(name="x", purpose="p", instructions="i.md", reads=(), artifacts=())
    assert (a.requires_api, a.fakes, a.phrases, a.budget) == (
        ">=2.0,<3",
        "fakes",
        "phrases.json",
        None,
    )


def test_extension_lists_contributions_not_hooks() -> None:
    ext = Extension(name="e", version="1", requires_api=">=1.0,<2")
    assert ext.tools == () and ext.systems == () and ext.egress == ()
    assert not any(callable(getattr(ext, f.name)) for f in fields(Extension))


def test_projection_wraps_a_reducer() -> None:
    p = Projection("trail", ProbeTrail())
    assert p.event_types == frozenset({"probe.noted", "artifact.saved"})
