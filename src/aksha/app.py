"""The composition root: the one place that picks adapters and wires them into the runtime.

It reads configuration (today: which storage adapter), builds each adapter, and hands it
to the runtime through the port it implements. The interfaces in :mod:`aksha.interfaces`
call it; nothing in :mod:`aksha.runtime` imports it. ADR-P20 decision 1 names this file.

Storage configuration, from the environment:

=====================  =====================================  =====================
Variable               Meaning                                Default
=====================  =====================================  =====================
``AKSHA_STORE``        ``sqlite`` or ``postgres``             ``sqlite``
``AKSHA_POSTGRES_DSN`` libpq DSN, needed for ``postgres``     none
``AKSHA_SQLITE_PATH``  the SQLite file                        ``var/aksha.sqlite``
``AKSHA_BLOB_PATH``    the filesystem blob store's folder     ``var/blobs``
=====================  =====================================  =====================

SQLite is the default so a run needs no setup: one file, no server (decision 34, 26 Nov
2026). PostgreSQL is the production backend (ADR-P01 D1): choose it with
``AKSHA_STORE=postgres`` and ``AKSHA_POSTGRES_DSN``. Both pass one contract suite. The
PostgreSQL driver is the optional extra ``aksha[postgres]``, imported only when PostgreSQL
is chosen, so the runtime itself stays standard library plus ``jsonschema``.

The blob store (decision 25, 26 Nov 2026) holds payload values over the 64 KiB cap and
large artifact bodies, content-addressed. The filesystem store is the only backend built;
an object-store backend for unattended runs comes later behind the same port.

Evals (ADR-P22): :func:`eval_agents` is the registry of agents an eval can put through an
episode, and :func:`eval_store_opener` opens each episode's and each results folder's
SQLite store on a fake clock fixed at the fixtures' frozen date, so two runs write the same
bytes. The real agents register in :func:`eval_agents`, one line each.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeGuard, cast

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.clock.system import SystemClock
from aksha.adapters.egress.network import HttpTransport, SocketResolver
from aksha.adapters.sandbox.bwrap import BwrapSandbox
from aksha.adapters.sources.folder import FolderSource, FolderSources
from aksha.adapters.sources.workspace import InputWorkspace
from aksha.adapters.storage import sqlite
from aksha.adapters.systems.mailbox import MailboxSystem
from aksha.evals.agents.runtime import EpisodeSource, RuntimeUnderTest
from aksha.evals.agents.scripted import ScriptedResearchAgent
from aksha.evals.case import Arm
from aksha.evals.judge import Judge, RuleJudge
from aksha.evals.ports import StoreOpener
from aksha.evals.registry import AgentRegistry
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.budgets.types import Limits
from aksha.runtime.context.reentry import ReentrySetting
from aksha.runtime.context.types import effective_policy
from aksha.runtime.definitions.hashing import run_started_fields
from aksha.runtime.definitions.loader import (
    LoadedDefinition,
    discover,
    load_agents,
    with_context_policy,
)
from aksha.runtime.definitions.modules import import_value
from aksha.runtime.definitions.names import qualify, resolve_kind
from aksha.runtime.definitions.types import Agent
from aksha.runtime.egress.ports import Network
from aksha.runtime.egress.types import EgressPolicy
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ids import LogIdSource
from aksha.runtime.execution.ports import Clock
from aksha.runtime.grounding.ports import RecordReader
from aksha.runtime.interrupts.approvals import ApprovalTool, approval_authority
from aksha.runtime.invalidation.types import VersionedSourceSpec
from aksha.runtime.loop.types import LoopSetup
from aksha.runtime.models.errors import BindRefusal, BindRefused
from aksha.runtime.models.routing import bind_aliases
from aksha.runtime.models.types import RunMode
from aksha.runtime.outbox.ports import SystemOfRecord
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.router.fake import PurposeClassifier
from aksha.runtime.router.router import Router
from aksha.runtime.sandbox.errors import SandboxRefused
from aksha.runtime.sandbox.ports import Sandbox
from aksha.runtime.sandbox.select import AUTO, select_backend
from aksha.runtime.sandbox.tier import ExecutionTier
from aksha.runtime.sandbox.types import STRENGTHS, Availability, EnvironmentSpec, Strength
from aksha.runtime.session.capabilities import CapabilityRecord, capabilities
from aksha.runtime.session.service import Session, SessionAgent
from aksha.runtime.storage.errors import StoreError
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import ToolDecl

Backend = Literal["sqlite", "postgres"]


@dataclass(frozen=True)
class StorageConfig:
    """Which event store backend to open, and where."""

    backend: Backend = "sqlite"
    postgres_dsn: str | None = None
    sqlite_path: Path = Path("var/aksha.sqlite")
    blob_path: Path = Path("var/blobs")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> StorageConfig:
        """Read the configuration from environment variables (see the module docstring)."""
        backend = env.get("AKSHA_STORE", "sqlite")
        if backend not in ("sqlite", "postgres"):
            raise StoreError("unknown_backend", f"AKSHA_STORE={backend!r}: use sqlite or postgres")
        return cls(
            backend=cast(Backend, backend),
            sqlite_path=Path(env.get("AKSHA_SQLITE_PATH", "var/aksha.sqlite")),
            postgres_dsn=env.get("AKSHA_POSTGRES_DSN"),
            blob_path=Path(env.get("AKSHA_BLOB_PATH", "var/blobs")),
        )


def open_event_store(
    config: StorageConfig,
    clock: Clock,
    *,
    writer: bool,
    catalogue: Catalogue | None = None,
) -> EventStore:
    """Open the configured event store. The PostgreSQL driver is imported only if chosen."""
    if config.backend == "sqlite":
        return sqlite.open_store(config.sqlite_path, clock, writer=writer, catalogue=catalogue)
    if not config.postgres_dsn:
        raise StoreError(
            "dsn_required",
            "AKSHA_STORE=postgres needs AKSHA_POSTGRES_DSN, a libpq connection string; "
            "leave AKSHA_STORE unset for the local SQLite file",
        )
    from aksha.adapters.storage import postgres

    return postgres.open_store(config.postgres_dsn, clock, writer=writer, catalogue=catalogue)


def open_blob_store(config: StorageConfig) -> BlobStore:
    """Open the blob store: a folder of content-addressed files at ``config.blob_path``."""
    return FilesystemBlobStore(config.blob_path)


# --- The session runtime (ADR-P19, decision 22) -------------------------------------------
#
# =======================  ==============================================  ==================
# Variable                 Meaning                                         Default
# =======================  ==============================================  ==================
# ``AKSHA_AGENTS``         folders holding agent folders (``os.pathsep``)  ``src/aksha/agents``
# ``AKSHA_EXTENSIONS``     folders holding extension folders               ``src/aksha/extensions``
# ``AKSHA_MODE``           run mode: ``fake`` (each agent's ``fakes.py``)  ``fake``
# ``AKSHA_SANDBOX``        the sandbox backend: ``auto``, ``bwrap``,       none
#                          ``refuse``, or ``<name>:<strength>``
# ``AKSHA_NETWORK``        ``on`` gives tools the real network under       ``off``
#                          their egress policies (ADR-P26)
# ``AKSHA_APPROVED_ENVIRONMENTS``  environment keys an operator granted   none
#                          a first build (ADR-P28 D4), ``,``-separated
# ``AKSHA_SERVER_URL``     the server a CLI command talks to               the server file
# ``AKSHA_SOURCES``        ``<source>=<folder>`` pairs (``os.pathsep``)    each spec's ``root``
# ``AKSHA_REENTRY``        re-entry setting a request run records:         ``fresh``
#                          ``fresh`` or ``resume`` (decision 36)
# ``AKSHA_SYSTEMS``        ``<system>=<kind>:<folder>`` pairs (``,``)      none
# =======================  ==============================================  ==================
#
# ``AKSHA_SOURCES`` names the folder each declared versioned source reads (ADR-P13 B1),
# for example ``inputs=fixtures/worlds/stillroom/sources`` for the research and patent
# agents, which both read the ``inputs`` source. Uploads (``aksha run --input``, ``aksha
# upload``, ``UploadInputs``) go to each owner's input workspace beside the SQLite file
# (``inputs/<owner>/``), which every run of that owner reads over the configured folder. A
# declared source with neither a setting nor a ``root`` has no reader: its tools read
# nothing and say so (``fetch_failed``). A setting naming no declared source is refused.
#
# Systems of record that extensions contribute (``Extension.systems``, ADR-P20 S2) are
# built here, once per full name (``<extension>.<name>``), by calling the spec's factory
# with ``state_dir``: a folder beside the SQLite file (``systems/<full name>``) where a
# local or fixture system may keep its own ledger. A real adapter may ignore it.
#
# ``AKSHA_SYSTEMS`` is opt-in and for development only. It binds a system of record that
# an effect names (``counsel_mail`` for the patent agent's ``send_packet``) or that an
# extension contributes (``<extension>.<name>``, replacing the extension's own) to a mock
# kind the composition root builds. The one kind is ``mailbox``: the ledgered mock mailbox
# ``make demo`` uses (``adapters.systems.mailbox``), a JSON file ``<folder>/mailbox.json``
# that delivers once per idempotency key and reaches no one. Nothing is bound by default,
# so an approval whose effect has no system is refused ``system_unavailable``; a name no
# loaded agent or extension uses is refused ``system_unknown``, an unknown kind
# ``system_invalid``. The CLI says on every command that the bound system is a mock.
#
# ``AKSHA_SANDBOX`` chooses tier 3's backend once, at start, from side-effect-free probes
# (ADR-P12 decision 2). ``bwrap`` (or ``bwrap:os-jail``) is the bubblewrap backend,
# ``auto`` the strongest built-in backend that probes available, ``refuse`` the backend
# that runs nothing. A built-in backend that does not probe available becomes ``refuse``
# with the probe's reason, never a weaker box. ``<name>:<strength>`` with any other name
# declares an outer box the operator attests the whole process runs in (a container,
# a VM): it lets a headless run start (ADR-P11 D12), and it runs no program, so an exec
# tool is still refused ``sandbox_unavailable``. Unset, no backend is chosen. Strength
# ``none`` is never a sandbox. The choice, every probe and the network setting are
# recorded on each ``run.started`` (decision 9).
#
# ``AKSHA_NETWORK`` is ``off`` unless set to ``on``: off, every tool's HTTP client
# refuses ``network_off``, so the default process is offline (ADR-P26 amendment A2).
# ``AKSHA_APPROVED_ENVIRONMENTS`` is the interim path for ADR-P28 D4: each listed key may
# be built once without the spec's pre-approval, and the grant is on ``run.started``.

DEFAULT_AGENTS = Path(__file__).resolve().parent / "agents"
DEFAULT_EXTENSIONS = Path(__file__).resolve().parent / "extensions"
SERVER_FILE = "aksha.server.json"
"""Beside the store: the URL of the server holding the writer lock, written by ``serve``."""
SYSTEMS_DIR = "systems"
"""Beside the store: one state folder per contributed system of record."""
MOCK_SYSTEM_KINDS = ("mailbox",)
"""The development systems of record ``AKSHA_SYSTEMS`` can bind (mocks, never real ones)."""


@dataclass(frozen=True)
class RuntimeConfig:
    """Everything the composition root needs to build a session, from the environment."""

    storage: StorageConfig = StorageConfig()
    agent_paths: tuple[Path, ...] = (DEFAULT_AGENTS,)
    extension_paths: tuple[Path, ...] = (DEFAULT_EXTENSIONS,)
    mode: str = "fake"
    sandbox: str | None = None
    server_url: str | None = None
    sources: Mapping[str, Path] = field(default_factory=dict)
    reentry: str = "fresh"
    systems: Mapping[str, tuple[str, Path]] = field(default_factory=dict)
    """``AKSHA_SYSTEMS``: each bound system's mock kind and folder, by system name."""
    network: str = "off"
    approved_environments: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> RuntimeConfig:
        """Read the configuration (see the table above)."""

        def paths(name: str, default: Path) -> tuple[Path, ...]:
            raw = env.get(name)
            if not raw:
                return (default,)
            return tuple(Path(p) for p in raw.split(os.pathsep) if p)

        return cls(
            storage=StorageConfig.from_env(env),
            agent_paths=paths("AKSHA_AGENTS", DEFAULT_AGENTS),
            extension_paths=paths("AKSHA_EXTENSIONS", DEFAULT_EXTENSIONS),
            mode=env.get("AKSHA_MODE", "fake"),
            sandbox=env.get("AKSHA_SANDBOX") or None,
            server_url=env.get("AKSHA_SERVER_URL") or None,
            sources=_source_settings(env.get("AKSHA_SOURCES", "")),
            reentry=env.get("AKSHA_REENTRY") or "fresh",
            systems=_system_settings(env.get("AKSHA_SYSTEMS", "")),
            network=env.get("AKSHA_NETWORK") or "off",
            approved_environments=tuple(
                k.strip()
                for k in env.get("AKSHA_APPROVED_ENVIRONMENTS", "").split(",")
                if k.strip()
            ),
        )

    def reentry_setting(self) -> ReentrySetting:
        """The re-entry setting (``AKSHA_REENTRY``) every request run records (decision 36).

        Fresh stays the default until the resume-against-fresh evaluation decides.
        """
        if self.reentry not in ("fresh", "resume"):
            raise StoreError(
                "reentry_invalid", f"AKSHA_REENTRY={self.reentry!r}: use fresh or resume"
            )
        return cast(ReentrySetting, self.reentry)

    def server_file(self) -> Path:
        """Where ``serve`` records its URL: beside the SQLite file."""
        return self.storage.sqlite_path.parent / SERVER_FILE

    def system_dir(self, name: str) -> Path:
        """The state folder of the contributed system of record ``name``."""
        return self.storage.sqlite_path.parent / SYSTEMS_DIR / name


def _source_settings(raw: str) -> dict[str, Path]:
    """``AKSHA_SOURCES``: ``name=folder`` pairs separated by ``os.pathsep``."""
    out: dict[str, Path] = {}
    for item in (part for part in raw.split(os.pathsep) if part):
        name, sep, folder = item.partition("=")
        if not sep or not name or not folder:
            raise StoreError("source_invalid", f"AKSHA_SOURCES item {item!r}: use name=folder")
        out[name] = Path(folder)
    return out


def _system_settings(raw: str) -> dict[str, tuple[str, Path]]:
    """``AKSHA_SYSTEMS``: ``name=kind:folder`` items separated by commas."""
    out: dict[str, tuple[str, Path]] = {}
    for item in (part.strip() for part in raw.split(",") if part.strip()):
        name, sep, rest = item.partition("=")
        kind, colon, folder = rest.partition(":")
        if not sep or not colon or not name or not kind or not folder:
            raise StoreError("system_invalid",
                             f"AKSHA_SYSTEMS item {item!r}: use name=mailbox:folder")  # fmt: skip
        if kind not in MOCK_SYSTEM_KINDS:
            raise StoreError("system_invalid", f"AKSHA_SYSTEMS item {item!r}: kind {kind!r} is "
                             f"not one of {list(MOCK_SYSTEM_KINDS)}")  # fmt: skip
        out[name] = (kind, Path(folder))
    return out


def mock_system_notes(config: RuntimeConfig) -> tuple[str, ...]:
    """One line per ``AKSHA_SYSTEMS`` binding, saying it is a mock, for every CLI command."""
    return tuple(
        f"mock: {name} is a development {kind} at {folder / 'mailbox.json'} (AKSHA_SYSTEMS); "
        "nothing it receives leaves this machine"
        for name, (kind, folder) in sorted(config.systems.items())
    )


def _is_strength(value: str) -> TypeGuard[Strength]:
    return value in STRENGTHS


def runtime_commit() -> tuple[str, bool]:
    """The git commit of the running code and whether its tree is dirty, when readable."""
    root = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown", False
    return commit.stdout.strip() or "unknown", bool(dirty.stdout.strip())  # fmt: skip


def _fake_adapters(loaded: LoadedDefinition) -> Mapping[str, object]:
    """The agent's ``fakes.py`` ``ADAPTERS``: the ``fake`` run mode's model per alias."""
    qualname = f"aksha_fakes_{loaded.agent.name}"
    found = import_value(loaded.folder, qualname, loaded.agent.fakes, "ADAPTERS")
    if not isinstance(found, Mapping):
        raise BindRefused(
            (BindRefusal("*", "fakes_missing", f"{loaded.module}: fakes.py has no ADAPTERS"),)
        )
    return found


def session_agent(
    loaded: LoadedDefinition,
    mode: str,
    adapters: Mapping[str, object] | None = None,
    *,
    commit: tuple[str, bool] = ("unknown", False),
    reentry: ReentrySetting | None = None,
) -> SessionAgent:
    """One loaded definition bound to its models, as a session runs it.

    ``reentry`` is the setting its request runs record (decision 36); None is ``fresh``.
    """
    agent = loaded.agent
    if adapters is None:
        if mode != "fake":
            refusal = BindRefusal("*", "mode_unsupported", f"AKSHA_MODE={mode!r}: only fake")
            raise BindRefused((refusal,))
        adapters = _fake_adapters(loaded)
    table = bind_aliases(loaded.model_aliases.values(), cast(RunMode, mode), adapters)
    briefs = {
        path: (loaded.folder / node.task.brief).read_text(encoding="utf-8")
        for path, node in compile_tree(agent.name, agent.tasks).nodes.items()
        if node.task.brief
    }
    extra = run_started_fields(loaded, runtime_commit=commit[0], dirty=commit[1])
    return SessionAgent(
        agent=agent,
        tree=compile_tree(agent.name, agent.tasks),
        registry=loaded.registry,
        table=table,
        definition_hash=loaded.definition_hash,
        instructions=(loaded.folder / agent.instructions).read_text(encoding="utf-8"),
        briefs=briefs,
        kinds=loaded.kinds,
        relations=loaded.relations,
        context_policy=agent.context_policy,
        run_extra=extra,
        reentry=reentry,
    )


def load_definitions(
    config: RuntimeConfig, *, catalogue: Catalogue | None = None
) -> tuple[LoadedDefinition, ...]:
    """Discover and load every agent the configuration names. Raises ``LoadError``."""
    found = discover(config.agent_paths, ())
    return load_agents(list(found), list(config.extension_paths), catalogue=catalogue)


def load_session_agents(
    config: RuntimeConfig,
    adapters: Mapping[str, Mapping[str, object]] | None = None,
    *,
    catalogue: Catalogue | None = None,
    loaded: Sequence[LoadedDefinition] | None = None,
) -> tuple[SessionAgent, ...]:
    """Discover, load and bind every agent the configuration names. Raises ``LoadError``.

    ``adapters`` overrides the model adapters of an agent by name (tests, scripted runs);
    ``loaded`` are definitions already loaded, so they are not loaded twice.
    """
    reentry = config.reentry_setting()
    if loaded is None:
        loaded = load_definitions(config, catalogue=catalogue)
    commit = runtime_commit()
    return tuple(
        session_agent(
            d, config.mode, (adapters or {}).get(d.agent.name), commit=commit, reentry=reentry
        )
        for d in loaded
    )


def extension_systems(
    config: RuntimeConfig,
    loaded: Sequence[LoadedDefinition],
    *,
    skip: Collection[str] = (),
) -> dict[str, SystemOfRecord]:
    """The systems of record the loaded agents' extensions contribute, by full name.

    Each is built once, by its spec's factory with its ``state_dir``; a name in ``skip``
    (one the caller supplies itself) is not built. Raises ``StoreError`` when a factory
    returns something that is not a system of record (``system_invalid``).
    """
    out: dict[str, SystemOfRecord] = {}
    for definition in loaded:
        for ext in definition.extensions:
            for spec in ext.ext.systems:
                name = qualify(ext.ext.name, spec.name)
                if name in out or name in skip:
                    continue
                built = spec.factory(state_dir=config.system_dir(name))
                if not all(callable(getattr(built, m, None)) for m in _SYSTEM_METHODS):
                    raise StoreError("system_invalid", f"{name}: the factory returned "
                                     f"{type(built).__name__}, not a system of record")  # fmt: skip
                out[name] = cast(SystemOfRecord, built)
    return out


_SYSTEM_METHODS = ("ledger_id", "apply", "lookup")


def effect_system_names(loaded: Sequence[LoadedDefinition]) -> frozenset[str]:
    """Every system of record the loaded agents' effects name or their extensions declare."""
    names: set[str] = set()
    for definition in loaded:
        for tool in definition.registry.names():
            spec = definition.registry.get(tool).spec
            if spec.external is not None:
                names.add(spec.external.system or spec.name)
        for ext in definition.extensions:
            names.update(qualify(ext.ext.name, spec.name) for spec in ext.ext.systems)
    return frozenset(names)


def configured_systems(
    config: RuntimeConfig, loaded: Sequence[LoadedDefinition]
) -> dict[str, SystemOfRecord]:
    """The mock systems ``AKSHA_SYSTEMS`` binds, by name (the table at the top).

    Raises ``StoreError`` ``system_unknown`` for a name no loaded agent or extension uses.
    """
    known = effect_system_names(loaded)
    unknown = sorted(set(config.systems) - known)
    if unknown:
        raise StoreError("system_unknown", f"AKSHA_SYSTEMS names {unknown}, which no loaded "
                         f"agent or extension uses; used: {sorted(known)}")  # fmt: skip
    return {
        name: MailboxSystem(folder / "mailbox.json", name)
        for name, (_, folder) in config.systems.items()
    }


INPUTS_SOURCE = "inputs"
"""The versioned source an agent declares to read a person's own uploaded files."""
INPUTS_DIR = "inputs"
"""Beside the store: each owner's uploaded inputs, ``inputs/<owner>/<path>``."""


def _declared_sources(loaded: Sequence[LoadedDefinition]) -> dict[str, VersionedSourceSpec]:
    declared: dict[str, VersionedSourceSpec] = {}
    for definition in loaded:
        for spec in definition.agent.sources:
            declared.setdefault(spec.name, spec)
        for ext in definition.extensions:
            for spec in ext.ext.sources:
                declared.setdefault(qualify(ext.ext.name, spec.name), spec)
    return declared


def _folder_of(config: RuntimeConfig, spec: VersionedSourceSpec, name: str) -> Path | None:
    return config.sources.get(name) or (Path(spec.root) if spec.root else None)


def source_reader(config: RuntimeConfig, loaded: Sequence[LoadedDefinition]) -> FolderSources:
    """The record reader over every declared versioned source that has a folder.

    A source's folder is its ``AKSHA_SOURCES`` setting, else its spec's ``root``. A source
    that declares one collection is served whole as that collection (``input/<path>``);
    one that declares several is served by top folder. Raises ``StoreError`` for a setting
    that names no declared source (``source_unknown``). The inputs source is served per
    owner by :func:`input_workspace` instead, over the same folder as its base.
    """
    declared = _declared_sources(loaded)
    unknown = sorted(set(config.sources) - set(declared))
    if unknown:
        raise StoreError("source_unknown", f"AKSHA_SOURCES names {unknown}, which no loaded "
                         f"agent declares; declared: {sorted(declared)}")  # fmt: skip
    held: dict[str, FolderSource] = {}
    for name, spec in declared.items():
        folder = _folder_of(config, spec, name)
        if folder is None:
            continue
        one = spec.collections[0] if len(spec.collections) == 1 else None
        held[name] = FolderSource(name, folder, one)
    return FolderSources.of(held)


def input_workspace(
    config: RuntimeConfig, loaded: Sequence[LoadedDefinition]
) -> InputWorkspace | None:
    """Each owner's uploaded inputs, when a loaded agent declares the inputs source.

    Uploads live beside the store (``inputs/<owner>/``); the source's configured folder
    (``AKSHA_SOURCES=inputs=<folder>``), if any, is what every owner sees as well.
    """
    spec = _declared_sources(loaded).get(INPUTS_SOURCE)
    if spec is None:
        return None
    collection = spec.collections[0] if spec.collections else "input"
    return InputWorkspace(INPUTS_SOURCE, config.storage.sqlite_path.parent / INPUTS_DIR,
                          collection=collection,
                          base=_folder_of(config, spec, INPUTS_SOURCE))  # fmt: skip


class ToolQueryRunner:
    """Re-executes a recorded query by calling the read tool that recorded it (ADR-P05 D6).

    A recorded query's ID is ``<agent>.<tool>?<canonical JSON arguments>``; its version is
    the result-set hash the tool returned as a ``query`` read. Re-executing it calls the same
    tool body again with the same arguments over the owner's current inputs, and answers
    the version it returns now. A query no loaded tool can answer keeps its last hash.
    """

    def __init__(self, loaded: Sequence[LoadedDefinition], reader: Callable[[str], Any]) -> None:
        """Answer queries of ``loaded``'s tools, reading through ``reader(owner)``."""
        self.loaded = {d.agent.name: d for d in loaded}
        self.reader = reader

    def __call__(self, owner: str, query: str) -> str:
        """The result-set hash of ``query`` for ``owner`` now."""
        from aksha.runtime.tools.types import Ok, ToolContext

        head, _, raw = query.partition("?")
        agent, _, tool = head.partition(".")
        definition = self.loaded.get(agent)
        if definition is None or tool not in definition.registry.names():
            return ""
        decl = definition.registry.get(tool)
        if decl.body is None:
            return ""
        args = json.loads(raw) if raw else {}
        ctx = ToolContext(owner, agent, "requery", None, f"requery/{query}", 1,
                          reader=self.reader(owner))  # fmt: skip
        outcome = decl.body(ctx, args)
        if not isinstance(outcome.result, Ok):
            return ""
        for read in outcome.result.reads:
            if read.kind == "query" and read.id == query:
                return read.version
        return ""


BUILT_IN_BACKENDS: Mapping[str, Callable[[], Sandbox]] = {"bwrap": BwrapSandbox}
"""The sandbox backends ``AKSHA_SANDBOX`` can choose, strongest last (ADR-P12)."""

_TIERS: dict[tuple[str | None, str, tuple[str, ...]], ExecutionTier] = {}
"""One probed tier per setting per process: probes are side-effect free and cached."""


def _choose_sandbox(
    setting: str | None,
) -> tuple[Sandbox | None, tuple[Availability, ...], Availability | None]:
    """``AKSHA_SANDBOX`` to (backend, probes, declared outer box); see the table above."""
    if setting is None:
        return None, (), None
    name, colon, strength = setting.partition(":")
    builtin = name in (AUTO, "refuse", *BUILT_IN_BACKENDS)
    if colon and not _is_strength(strength):
        raise StoreError(
            "sandbox_invalid", f"AKSHA_SANDBOX={setting!r}: strength is one of {STRENGTHS}"
        )
    if not builtin:
        if not colon:
            names = ", ".join((AUTO, "refuse", *BUILT_IN_BACKENDS))
            raise StoreError("sandbox_invalid", f"AKSHA_SANDBOX={setting!r}: use {names}, "
                             "or <name>:<strength> for an outer box")  # fmt: skip
        usable = strength != "none"
        why = "" if usable else "strength none is not a sandbox"
        return None, (), Availability(name, cast(Strength, strength), usable, why)
    try:
        box, probes = select_backend(name, BUILT_IN_BACKENDS)
    except SandboxRefused as refused:
        raise StoreError("sandbox_invalid", refused.message) from refused
    if colon and name in BUILT_IN_BACKENDS and strength != BUILT_IN_BACKENDS[name]().strength:
        raise StoreError("sandbox_invalid", f"AKSHA_SANDBOX={setting!r}: {name} is "
                         f"{BUILT_IN_BACKENDS[name]().strength}")  # fmt: skip
    return box, probes, None


def execution_tier(config: RuntimeConfig, loaded: Sequence[LoadedDefinition]) -> ExecutionTier:
    """Tier 3 for ``config``: the backend, the network, egress policies and environments.

    The egress policies and environment specs are the loaded extensions', by full name
    (``<extension>.<name>``); an environment's ``egress`` is qualified by its extension.
    """
    if config.network not in ("on", "off"):
        raise StoreError("network_invalid", f"AKSHA_NETWORK={config.network!r}: on or off")
    for key in config.approved_environments:
        if not (key.startswith("sha256:") and len(key) == 71):
            raise StoreError("environment_key_invalid", f"{key!r} is not an environment key")
    cache_key = (config.sandbox, config.network, config.approved_environments)
    base = _TIERS.get(cache_key)
    if base is None:
        box, probes, declared = _choose_sandbox(config.sandbox)
        network = Network(SocketResolver(), HttpTransport()) if config.network == "on" else None
        base = ExecutionTier(box=box, probes=probes, network=network, declared=declared,
                             approved_images=frozenset(config.approved_environments))  # fmt: skip
        _TIERS[cache_key] = base
    egress: dict[str, EgressPolicy] = {}
    environments: dict[str, EnvironmentSpec] = {}
    for definition in loaded:
        for ext in definition.extensions:
            for policy in ext.ext.egress:
                egress[qualify(ext.ext.name, policy.name)] = policy
            for env in ext.ext.environments:
                environments[qualify(ext.ext.name, env.name)] = replace(
                    env, egress=resolve_kind(env.egress, ext.ext.name)
                )
    return replace(base, egress=egress, environments=environments)


def open_session(
    config: RuntimeConfig,
    clock: Clock | None = None,
    *,
    adapters: Mapping[str, Mapping[str, object]] | None = None,
    systems: Mapping[str, SystemOfRecord] | None = None,
) -> Session:
    """The writer's session: store (writer lock), agents, router, sandbox, systems, reader.

    The systems are the extensions' (:func:`extension_systems`), then the mocks
    ``AKSHA_SYSTEMS`` binds (:func:`configured_systems`), then ``systems`` (tests,
    scripted runs), each replacing the one before by name. Raises ``LockHeld`` when another
    process holds the writer lock (a server, or another embedded command).
    """
    clock = clock or SystemClock()
    catalogue = Catalogue(CORE_EVENT_TYPES)
    loaded = load_definitions(config, catalogue=catalogue)
    agents = load_session_agents(config, adapters, loaded=loaded)
    given = {**configured_systems(config, loaded), **(systems or {})}
    every_system = {**extension_systems(config, loaded, skip=given), **given}
    reader = source_reader(config, loaded)
    inputs = input_workspace(config, loaded)
    store = open_event_store(config.storage, clock, writer=True, catalogue=catalogue)
    ids = LogIdSource(store)
    router = None
    if len(agents) >= 2:
        routable = tuple(a.routable() for a in agents)
        router = Router(routable, PurposeClassifier(), ids)
    tier = execution_tier(config, loaded)
    sandbox = tier.gate_availability
    recorded = tier.record()
    agents = tuple(replace(a, run_extra={**a.run_extra, "sandbox": recorded}) for a in agents)
    return Session(store, clock, agents, router=router, sandbox=lambda: sandbox,
                   blobs=open_blob_store(config.storage), systems=every_system,
                   reader=reader, ids=ids, tier=tier, inputs=inputs, rebuilds=True,
                   run_query=None if inputs is None else ToolQueryRunner(
                       loaded, lambda owner: inputs.reader(owner)))  # fmt: skip


def open_reader(config: RuntimeConfig, clock: Clock | None = None) -> EventStore:
    """A read-only handle on the configured store: never takes the lock, never boots."""
    return open_event_store(config.storage, clock or SystemClock(), writer=False)


def session_capabilities(session: Session, config: RuntimeConfig) -> CapabilityRecord:
    """The capability record of ``session`` (ADR-P19 D7) with this process's commit."""
    commit, dirty = runtime_commit()
    sandbox = session.tier.gate_availability
    backends = () if sandbox is None or not sandbox.available else (sandbox.backend,)
    return capabilities(session, runtime_commit=commit, dirty=dirty, sandbox_backends=backends)


def export_trace(
    store: EventStore, owner: str, run: str, out_dir: Path, *, store_id: str | None = None
) -> tuple[Path, ...]:
    """Export ``run``'s trace as OTLP JSON Lines files, one per segment (ADR-P21 D8).

    The exporter is the ``otel`` extension; it needs the optional extra ``aksha[otel]``
    and raises ``ExporterMissing`` with an install hint without it.
    """
    from aksha.extensions.otel.export import export_run

    events = [e for e in store.read(owner) if e.run == run]
    return export_run(events, run, out_dir, store_id=store_id or store.identity().store_id)


# --- The offline demo's composition (``make demo``) ---------------------------------------
#
# The demo (``aksha.interfaces.demo``) runs the research agent and the patent agent on one
# owner's SQLite store, offline: a fixed clock, each agent's rule-based fake model, the
# fixture world's ``sources/`` folder copied into the demo's work folder (the only folder a
# tool reads), and a ledgered mock mailbox standing in for counsel. Nothing here reaches a
# network or needs a key.

DEMO_START = datetime(2026, 11, 26, 9, 0, 0, tzinfo=UTC)
"""The demo's fixed clock: every run of the demo records the same times."""
DEMO_OWNER = "founder-1"
DEMO_AGENT_NAMES = ("research", "patent")
COUNSEL_MAILBOX = "counsel_mail"
"""The system name the patent agent's ``send_packet`` effect is sent to."""


@dataclass
class DemoRuntime:
    """One process's view of the demo: the store, both agents' loop setups, the outbox."""

    store: EventStore
    clock: FakeClock
    research: LoopSetup
    patent: LoopSetup
    patent_source: FolderSource
    run_query: ToolQueryRunner
    send_tools: Mapping[str, ApprovalTool]
    outbox: OutboxWorker
    mailbox: SystemOfRecord
    loaded: Mapping[str, str]
    """Each agent's definition hash, as recovery checks it against ``run.started``."""

    def executor(self) -> StepExecutor:
        """A step executor for the owner, sending effects through the outbox."""
        return StepExecutor(self.store, DEMO_OWNER, self.clock, outbox=self.outbox)

    def setup(self, agent: str) -> LoopSetup:
        """The loop setup of ``agent``."""
        return self.research if agent == self.research.agent.name else self.patent

    def close(self) -> None:
        """Close the store (and give up the writer lock)."""
        self.store.close()


def counsel_mailbox(work: Path) -> MailboxSystem:
    """The demo's mock counsel mailbox: a JSON ledger in ``work``."""
    return MailboxSystem(work / "mailbox.json", COUNSEL_MAILBOX)


def _briefs(folder: Path, agent: Agent) -> dict[str, str]:
    return {
        path: (folder / node.task.brief).read_text(encoding="utf-8")
        for path, node in compile_tree(agent.name, agent.tasks).nodes.items()
        if node.task.brief
    }


def _setup(
    store: EventStore,
    loaded: LoadedDefinition,
    agent: Agent,
    registry: ToolRegistry,
    model: object,
    request: str,
    blobs: BlobStore,
    reader: RecordReader | None = None,
) -> LoopSetup:
    kinds = loaded.kinds
    return LoopSetup(
        store=store,
        owner=DEMO_OWNER,
        agent=agent,
        tree=compile_tree(agent.name, agent.tasks),
        registry=registry,
        table=bind_aliases(agent.model_aliases, "fake", {"generate": model}),
        instructions=(loaded.folder / agent.instructions).read_text(encoding="utf-8"),
        request=request,
        briefs=_briefs(loaded.folder, agent),
        context_policy=agent.context_policy,
        artifacts=ArtifactStore(kinds, loaded.relations, blobs=blobs),
        kinds=kinds,
        definition_hash=loaded.definition_hash,
        limits=agent.limits or Limits(),
        blobs=blobs,
        reader=reader,
    )


def open_demo(
    work: Path,
    sources: Path,
    requests: Mapping[str, str],
    *,
    mailbox: SystemOfRecord | None = None,
) -> DemoRuntime:
    """Open the demo's store in ``work`` and wire both agents over the ``sources`` folder.

    ``requests`` holds each agent's request text by agent name. ``mailbox`` replaces the
    counsel mailbox (the demo's kill step wraps it); by default it is :func:`counsel_mailbox`.
    The agents' modules are imported here, so importing this module stays light.
    """
    from aksha.agents.patent.agent import build_agent as build_patent_agent
    from aksha.agents.patent.fakes import PatentFake
    from aksha.agents.patent.world import SourceWorld
    from aksha.agents.research.fakes import ResearchModel

    clock = FakeClock(DEMO_START)
    store = sqlite.open_store(work / "aksha.sqlite", clock, writer=True)
    blobs = FilesystemBlobStore(work / "blobs")
    loaded = {
        d.agent.name: d for d in load_agents([DEFAULT_AGENTS / n for n in DEMO_AGENT_NAMES], [])
    }
    research, patent = loaded["research"], loaded["patent"]
    folder = FolderSource(INPUTS_SOURCE, sources, "input")
    research_setup = _setup(
        store, research, research.agent, research.registry, ResearchModel(),
        requests[research.agent.name], blobs, folder,
    )  # fmt: skip
    world = SourceWorld(DEMO_START.date())
    agent = build_patent_agent(world)
    listed = {t.spec.name: t for t in agent.tools if isinstance(t, ToolDecl)}
    patent_setup = _setup(
        store, patent, agent, ToolRegistry(agent, listed, ()), PatentFake(),
        requests[agent.name], blobs, folder,
    )  # fmt: skip
    send_tools = {"send_packet": ApprovalTool(listed["send_packet"].spec)}
    hashes = {research.agent.name: research.definition_hash, agent.name: patent.definition_hash}
    authority = approval_authority(send_tools, lambda entry: patent.definition_hash)
    box = mailbox or counsel_mailbox(work)
    worker = OutboxWorker(store, DEMO_OWNER, clock, {COUNSEL_MAILBOX: box}, authority=authority)
    queries = ToolQueryRunner(list(loaded.values()), lambda owner: folder)
    return DemoRuntime(
        store,
        clock,
        research_setup,
        patent_setup,
        folder,
        queries,
        send_tools,
        worker,
        box,
        hashes,
    )


EVAL_CLOCK_START = datetime(2026, 11, 26, tzinfo=UTC)
"""Where an eval's fake clock starts: the fixtures' ``frozen_today``."""


def eval_store_opener() -> StoreOpener:
    """Opens an eval store (an episode's or a results folder's) as SQLite on a fake clock."""

    def open_store(path: Path, catalogue: Catalogue) -> EventStore:
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite.open_store(
            path, FakeClock(EVAL_CLOCK_START), writer=True, catalogue=catalogue
        )

    return open_store


class _RuntimeAdapter:
    """Builds one runtime agent's eval adapter per episode, over sessions built here.

    The adapter (``aksha.evals.agents.<name>``) drives a ``Session`` this factory builds in
    the episode's ``runtime/`` folder: a SQLite store on the eval clock, a filesystem blob
    store, the agent loaded from ``agents/<name>`` and bound to its fakes, the adapter's
    reader over the episode and its systems of record. ``definition_hash`` is the loaded
    definition's, so result rows name the agent definition, not the adapter's source.
    """

    def __init__(self, adapter: type[RuntimeUnderTest]) -> None:
        """Load ``adapter``'s agent once."""
        self.adapter = adapter
        catalogue = Catalogue(CORE_EVENT_TYPES)
        (self.loaded,) = load_agents([DEFAULT_AGENTS / adapter.agent], [], catalogue=catalogue)
        self.catalogue = catalogue
        self.definition_hash = self.loaded.definition_hash
        self.bound = session_agent(self.loaded, "fake", commit=runtime_commit())
        self._arms: dict[str, tuple[SessionAgent, str]] = {}

    def __call__(self) -> RuntimeUnderTest:
        """A fresh adapter for one episode."""
        return self.adapter(self.session)

    def for_arm(self, arm: Arm) -> RuntimeUnderTest:
        """A fresh adapter for one episode of ``arm``: its re-entry setting and context policy.

        The arm's ``reentry`` is what the agent's request runs record (decision 36); its
        ``premise_exempt`` kinds are added to the agent's resume policy, which changes the
        definition hash, so the adapter declares the arm's hash on its result rows.
        """
        if arm.name not in self._arms:
            loaded = self.loaded
            if arm.premise_exempt:
                base = effective_policy(loaded.agent.context_policy)
                resume = replace(base.resume, premise_exempt=frozenset(arm.premise_exempt))
                loaded = with_context_policy(loaded, replace(base, resume=resume))
            reentry = cast(ReentrySetting | None, arm.reentry)
            bound = session_agent(loaded, "fake", commit=runtime_commit(), reentry=reentry)
            self._arms[arm.name] = (bound, loaded.definition_hash)
        bound, digest = self._arms[arm.name]

        def build(folder: Path, reader: RecordReader, systems: Mapping[str, object],
                  source: EpisodeSource) -> Session:  # fmt: skip
            return self._session(bound, folder, reader, systems, source)

        return self.adapter(build, digest)

    def session(
        self,
        folder: Path,
        reader: RecordReader,
        systems: Mapping[str, object],
        source: EpisodeSource,
    ) -> Session:
        """The writer session of one episode's runtime store in ``folder``."""
        return self._session(self.bound, folder, reader, systems, source)

    def _session(
        self,
        bound: SessionAgent,
        folder: Path,
        reader: RecordReader,
        systems: Mapping[str, object],
        source: EpisodeSource,
    ) -> Session:
        folder.mkdir(parents=True, exist_ok=True)
        clock = FakeClock(EVAL_CLOCK_START)
        store = sqlite.open_store(folder / "aksha.sqlite", clock, writer=True,
                                  catalogue=self.catalogue)  # fmt: skip
        sources = (source,) if source.collections else ()
        return Session(store, clock, (bound,), blobs=FilesystemBlobStore(folder / "blobs"),
                       systems=cast(Mapping[str, SystemOfRecord], systems),
                       sources=sources, reader=reader)  # fmt: skip


def eval_agents() -> AgentRegistry:
    """The agents an eval can run, by the name cases list them under.

    The registration hook for the real agents: each adapter in ``aksha/evals/agents/``
    registers here. ``research`` and ``patent`` run the shipped agents on the runtime with
    their fake models (:class:`_RuntimeAdapter`); ``scripted`` is the runner's own test
    agent. A case naming an agent that is not registered is refused ``agent_not_registered``.
    """
    from aksha.evals.agents.patent import PatentUnderTest
    from aksha.evals.agents.research import ResearchUnderTest

    registry = AgentRegistry()
    registry.register(
        "scripted",
        ScriptedResearchAgent,
        mode="fake",
        description="small rule-based research agent over competitor pages and reviews",
    )
    research = _RuntimeAdapter(ResearchUnderTest)
    registry.register(
        "research",
        research,
        mode="fake",
        description="the research agent on the runtime, rule-based fake model",
        armed=research.for_arm,
    )
    patent = _RuntimeAdapter(PatentUnderTest)
    registry.register(
        "patent",
        patent,
        mode="fake",
        description="the patent agent on the runtime, rule-based fake model, mock counsel mail",
        armed=patent.for_arm,
    )
    return registry


def eval_judge() -> Judge:
    """The fake judge for fake model mode: rule-based, offline, annotate-only."""
    return Judge(model=RuleJudge(), model_id="fake:RuleJudge")
