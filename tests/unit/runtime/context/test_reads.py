from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from aksha.runtime.artifacts.types import ArtifactKind
from aksha.runtime.context.reads import declared_versions, fill_template, render_reads
from aksha.runtime.context.types import ContextPolicy
from aksha.runtime.tasks.types import InstanceRef, Read, from_field
from tests.support.context import RUN, Log, mint_and_cite, tree

EDIT = InstanceRef(RUN, "edit", "parser")
PLAN_STEP = f"{RUN}/plan/t01/c01"
POLICY = ContextPolicy()
KINDS = {
    "coder.plan": ArtifactKind("plan", {"type": "object"}),
    "coder.family": ArtifactKind(
        "family", {"type": "object"}, keyed=True, summary="Family {name}: {claims}"
    ),
}


@pytest.fixture
def log(tmp_path: Path) -> Iterator[Log]:
    log = Log(tmp_path / "aksha.sqlite")
    log.start()
    yield log
    log.close()


def test_fill_template_inserts_top_level_fields() -> None:
    content = {"name": "E", "claims": ["c1", "c2"], "n": 3}
    assert fill_template("{name} has {claims}, {n} and {missing}", content) == (
        'E has ["c1","c2"], 3 and {missing}'
    )


def test_own_read_renders_the_current_version_whole(log: Log) -> None:
    log.save("coder.plan@v1", {"files": ["parser"], "goal": "old"}, step=PLAN_STEP)
    log.save("coder.plan@v2", {"files": ["parser"], "goal": "new"}, step=PLAN_STEP)
    out = render_reads(log.view(), tree(("coder.plan",)), EDIT, POLICY, KINDS)
    assert [(r.ref, r.mode) for r in out.rendered] == [("coder.plan@v2", "full")]
    assert '"goal":"new"' in out.text and '"goal":"old"' not in out.text
    assert [str(i) for i in out.inputs] == ["artifact:coder.plan@v2"]


def test_no_declared_read_renders_nothing(log: Log) -> None:
    out = render_reads(log.view(), tree(), EDIT, POLICY, KINDS)
    assert (out.text, out.rendered, out.inputs) == ("", (), ())


def test_read_over_cap_renders_summary(log: Log) -> None:
    claims = ["claim " + "x" * 500 for _ in range(30)]
    log.save("coder.family/E@v1", {"name": "E", "claims": claims, "body": "b" * 14_000},
             step=PLAN_STEP)  # fmt: skip
    reads = (Read("coder.family", "all"),)
    out = render_reads(log.view(), tree(reads), EDIT, POLICY, KINDS)
    (r,) = out.rendered
    assert (r.ref, r.mode) == ("coder.family/E@v1", "summary")
    assert "Family E:" in out.text and "b" * 100 not in out.text
    assert r.chars <= POLICY.read_cap_chars + 100
    assert [str(i) for i in out.inputs] == ["artifact:coder.family/E@v1"]


def test_read_over_cap_without_template_is_cut_head_and_tail(log: Log) -> None:
    log.save("coder.plan@v1", {"files": ["parser"], "body": "h" * 9_000 + "t" * 9_000},
             step=PLAN_STEP)  # fmt: skip
    out = render_reads(log.view(), tree(("coder.plan",)), EDIT, POLICY, KINDS)
    assert out.rendered[0].mode == "summary"
    assert "characters cut" in out.text and "hhh" in out.text and "ttt" in out.text


def test_read_block_cap_turns_the_rest_into_refs(log: Log) -> None:
    for key in ("A", "B", "C", "D"):
        log.save(f"coder.family/{key}@v1", {"name": key, "claims": "x" * 9_000},
                 step=PLAN_STEP)  # fmt: skip
    policy = replace(POLICY, read_block_cap_chars=25_000)
    out = render_reads(log.view(), tree((Read("coder.family", "all"),)), EDIT, policy, KINDS)
    assert [r.mode for r in out.rendered] == ["full", "full", "ref_only", "ref_only"]
    assert [str(i) for i in out.inputs] == [
        "artifact:coder.family/A@v1",
        "artifact:coder.family/B@v1",
    ]
    assert "[artifact coder.family/D@v1, ref_only]" in out.text


def test_from_field_reads_the_listed_keys(log: Log) -> None:
    log.save("coder.plan@v1", {"families": ["B"]}, step=PLAN_STEP)
    log.save("coder.family/A@v1", {"name": "A", "claims": []}, step=PLAN_STEP)
    log.save("coder.family/B@v1", {"name": "B", "claims": []}, step=PLAN_STEP)
    reads = ("coder.plan", Read("coder.family", from_field("coder.plan", "families")))
    found = declared_versions(log.view(), tree(reads), EDIT)
    assert [str(v.ref) for v in found] == ["coder.plan@v1", "coder.family/B@v1"]


def test_rendering_names_the_spans_whose_text_it_showed(log: Log) -> None:
    claims = ["F3 is disclosed in E"] + ["filler " + "y" * 400 for _ in range(40)]
    log.save("coder.family/E@v1", {"name": "E", "claims": claims}, step=PLAN_STEP)
    mint_and_cite(
        log, "coder.family/E@v1",
        (("EX-0001", "F3 is disclosed in E"), ("EX-0002", "a sentence the summary drops")),
        f"{RUN}/scan/t01/c01",
    )  # fmt: skip
    template = "Family {name}, first claim {claims}"
    kinds = {
        "coder.family": ArtifactKind("family", {"type": "object"}, keyed=True, summary=template)
    }
    out = render_reads(log.view(), tree((Read("coder.family", "all"),)), EDIT, POLICY, kinds)
    (r,) = out.rendered
    assert r.mode == "summary"
    assert r.spans == frozenset({"EX-0001"})


def test_a_full_rendering_shows_every_cited_span(log: Log) -> None:
    log.save("coder.family/E@v1", {"name": "E", "claims": ["short"]}, step=PLAN_STEP)
    mint_and_cite(log, "coder.family/E@v1", (("EX-0001", "short"), ("EX-0002", "other")),
                  f"{RUN}/scan/t01/c01")  # fmt: skip
    out = render_reads(log.view(), tree((Read("coder.family", "all"),)), EDIT, POLICY, KINDS)
    assert out.rendered[0].spans == frozenset({"EX-0001", "EX-0002"})
