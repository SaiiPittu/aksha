"""The ADR-P20 boundary guards, each with a companion that plants a violation and sees it caught.

1. A third agent and a third extension import only ``aksha.runtime.api`` and load from a
   temporary folder with no runtime file changed (the runtime file hash is equal before
   and after).
2. The runtime holds no agent word (the static term check).
3. No stop policy class outside the runtime: the loader refuses one.
4. The extension API's names and signatures equal the committed snapshot.
5. Every contributed projection is pure: two cold rebuilds give equal results.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from aksha.runtime import api
from aksha.runtime.definitions.errors import LoadError
from aksha.runtime.definitions.loader import load_agents, load_extensions
from aksha.runtime.definitions.modules import scan_sources
from aksha.runtime.definitions.surface import api_surface, required_bump
from aksha.runtime.definitions.versions import parse_requirement
from aksha.runtime.events.envelope import Event
from aksha.runtime.projections.project import rebuild
from aksha.runtime.projections.view import ArtifactsView, LogView
from tests.architecture.rules import agent_terms_in, check_dependency_rule
from tests.support.agents import FIXTURES, Plugins, copy_plugins

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"  # the source root: the folder that holds the aksha package
SNAPSHOT = Path(__file__).with_name("api_surface.json")
TERMS = FIXTURES / "agent_terms.txt"


def _runtime_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted((SRC / "aksha" / "runtime").rglob("*.py")):
        digest.update(str(path.relative_to(SRC)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture
def plugins(tmp_path: Path) -> Plugins:
    return copy_plugins(tmp_path)


# --- 1. A third agent and extension need no runtime edit --------------------------------


def test_conformance_fixtures_import_only_the_api() -> None:
    """The real guarantee behind "a third agent needs no core edit", until the loop exists.

    The conformance agent and extension live outside ``aksha/runtime`` and reach it only
    through ``aksha.runtime.api`` (the strict scan; its planted companions are in
    ``tests/unit/runtime/definitions/test_loader.py``). When the agent loop (ADR-P06) lands,
    the two hash tests below must run a turn, not only load.
    """
    assert scan_sources(FIXTURES / "agents" / "toy") == ()
    assert scan_sources(FIXTURES / "extensions" / "echo") == ()
    for folder in (FIXTURES / "agents" / "toy", FIXTURES / "extensions" / "echo"):
        assert not folder.resolve().is_relative_to(SRC / "aksha" / "runtime")


def test_third_agent_runtime_hash_unchanged(plugins: Plugins) -> None:
    """The conformance agent loads from a temporary folder; no runtime file changes.

    This can only fail if loading writes into the runtime. "Runs" in the ADR's wording waits
    for the agent loop (ADR-P06); the import check above carries the guarantee until then.
    """
    before = _runtime_hash()
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    assert loaded.agent.name == "toy"
    assert _runtime_hash() == before


def test_third_extension_runtime_hash_unchanged(plugins: Plugins) -> None:
    before = _runtime_hash()
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    lookup = loaded.tools["echo.lookup"]
    ctx = api.ToolContext(owner="o", agent="toy", task_path="collect", key=None, step_id="s",
                          attempt=1)  # fmt: skip
    assert lookup.body is not None
    assert lookup.body(ctx, {"text": "hi"}) == api.ToolOutcome(api.Ok({"text": "hi"}))
    counter = loaded.projections["echo.runs_started"]
    assert rebuild(counter.reducer, _log()) == 2
    assert _runtime_hash() == before


def test_runtime_imports_only_stdlib_and_jsonschema() -> None:
    rule = "runtime imports the standard library and jsonschema only"
    assert [str(v) for v in check_dependency_rule(SRC) if v.rule == rule] == []


# --- 2. No agent word in the runtime ----------------------------------------------------


def _terms() -> tuple[str, ...]:
    lines = TERMS.read_text(encoding="utf-8").splitlines()
    return tuple(t.strip() for t in lines if t.strip() and not t.startswith("#"))


def test_runtime_has_no_agent_terms() -> None:
    assert [str(v) for v in agent_terms_in(SRC, _terms())] == []


def test_term_check_catches_a_planted_term(tmp_path: Path) -> None:
    shutil.copytree(SRC / "aksha", tmp_path / "aksha", ignore=shutil.ignore_patterns("__pycache__"))
    target = tmp_path / "aksha" / "runtime" / "planted.py"
    target.write_text('"""Planted."""\n\nNEEDS = "needs_scope"\n')
    found = agent_terms_in(tmp_path, _terms())
    assert [(v.module, v.target) for v in found] == [("aksha.runtime.planted", "needs_scope")]


# --- 3. The stop-policy set is closed ---------------------------------------------------


def test_no_director_subclass_outside_runtime(plugins: Plugins) -> None:
    load_agents([plugins.toy], [plugins.extensions])  # the clean copy loads
    (plugins.toy / "director.py").write_text(
        '"""A planted stop policy."""\n\n\nclass Finisher:\n'
        "    def prepare(self, view: object) -> None: ...\n\n"
        "    def judge(self, view: object) -> None: ...\n"
    )
    with pytest.raises(LoadError) as err:
        load_agents([plugins.toy], [plugins.extensions])
    first = err.value.refusals[0]
    assert (first.code, first.module, first.field) == (
        "stop_policy_class",
        "toy/director.py",
        "classes",
    )


def test_extension_stop_policy_class_is_refused(plugins: Plugins) -> None:
    (plugins.echo / "policy.py").write_text(
        '"""Planted."""\n\n\nclass Mine(StopPolicy):  # noqa: F821\n    pass\n'
    )
    with pytest.raises(LoadError) as err:
        load_extensions(["echo"], [plugins.extensions])
    assert err.value.refusals[0].code == "stop_policy_class"


# --- 4. The extension API snapshot ------------------------------------------------------


def _snapshot() -> dict[str, object]:
    data: dict[str, object] = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return data


def _write_snapshot_if_asked() -> None:
    """With ``AKSHA_UPDATE_API_SNAPSHOT=1``, rewrite the snapshot, but only after a version bump."""
    if os.environ.get("AKSHA_UPDATE_API_SNAPSHOT") != "1":
        return
    old = _snapshot() if SNAPSHOT.exists() else {"api_version": "0.0", "surface": {}}
    surface = old["surface"]
    assert isinstance(surface, dict)
    bump = required_bump(surface, api_surface())
    assert _bumped(str(old["api_version"]), api.API_VERSION, bump), (
        f"the API changed ({bump}); bump API_VERSION before updating the snapshot"
    )
    SNAPSHOT.write_text(
        json.dumps(
            {"api_version": api.API_VERSION, "surface": api_surface()}, indent=2, sort_keys=True
        )
        + "\n",
        encoding="utf-8",
    )


def _bumped(old: str, new: str, bump: str) -> bool:
    (_, o), (_, n) = parse_requirement(f"=={old}")[0], parse_requirement(f"=={new}")[0]
    if bump == "none":
        return True
    if bump == "major":
        return n[0] > o[0]
    return n > o


def test_extension_api_surface_unchanged() -> None:
    _write_snapshot_if_asked()
    snap = _snapshot()
    assert snap["api_version"] == api.API_VERSION, "API_VERSION changed: update the snapshot"
    assert snap["surface"] == api_surface(), (
        "the extension API changed: bump API_VERSION (minor to add, major to change or remove), "
        "then run AKSHA_UPDATE_API_SNAPSHOT=1 make test"
    )


def test_snapshot_guard_catches_a_changed_signature() -> None:
    changed = dict(api_surface())
    changed["source_keys"] = "def source_keys(collection: 'str', extra: 'int') -> 'KeySource'"
    assert changed != _snapshot()["surface"]
    assert required_bump(api_surface(), changed) == "major"
    assert not _bumped("1.0", "1.1", "major") and _bumped("1.0", "2.0", "major")


# --- 5. Contributed projections are pure ------------------------------------------------


def _event(seq: int, type_: str) -> Event:
    payload = {"agent": "a", "cause": "request"} if type_ == "run.started" else {}
    return Event(
        type=type_,
        payload=payload,
        run=f"R-{seq}",
        seq=seq,
        owner="o1",
        at="",
        boot=1,
        schema_version=1,
    )


def _log() -> LogView:
    events = (_event(1, "run.started"), _event(2, "probe.noted"), _event(3, "run.started"))
    return LogView(owner="o1", seq_read=3, events=events, artifacts=ArtifactsView((), 3))


def test_contributed_reducer_is_pure(plugins: Plugins) -> None:
    (loaded,) = load_agents([plugins.toy], [plugins.extensions])
    for projection in loaded.projections.values():
        assert rebuild(projection.reducer, _log()) == rebuild(projection.reducer, _log())
