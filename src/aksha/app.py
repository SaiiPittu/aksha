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

import os
import subprocess
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeGuard, cast

from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.clock.system import SystemClock
from aksha.adapters.sources.folder import FolderSource, FolderSources
from aksha.adapters.storage import sqlite
from aksha.adapters.systems.mailbox import MailboxSystem
from aksha.evals.agents.scripted import ScriptedResearchAgent
from aksha.evals.judge import Judge, RuleJudge
from aksha.evals.ports import StoreOpener
from aksha.evals.registry import AgentRegistry
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.budgets.types import Limits
from aksha.runtime.definitions.hashing import run_started_fields
from aksha.runtime.definitions.loader import LoadedDefinition, discover, load_agents
from aksha.runtime.definitions.modules import import_value
from aksha.runtime.definitions.names import qualify
from aksha.runtime.definitions.types import Agent
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
from aksha.runtime.router.types import RoutableAgent
from aksha.runtime.sandbox.types import STRENGTHS, Availability, Strength
from aksha.runtime.session.capabilities import CapabilityRecord, capabilities
from aksha.runtime.session.service import Session, SessionAgent
from aksha.runtime.storage.errors import StoreError
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import ToolDecl

if TYPE_CHECKING:
    from aksha.agents.patent.world import SourceWorld

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
# ``AKSHA_SANDBOX``        ``<backend>:<strength>`` the operator provides  none
# ``AKSHA_SERVER_URL``     the server a CLI command talks to               the server file
# ``AKSHA_SOURCES``        ``<source>=<folder>`` pairs (``os.pathsep``)    each spec's ``root``
# =======================  ==============================================  ==================
#
# ``AKSHA_SOURCES`` names the folder each declared versioned source reads (ADR-P13 B1),
# for example ``corpus=fixtures/worlds/stillroom/sources`` for the research agent. A
# declared source with neither a setting nor a ``root`` has no reader: its tools read
# nothing and say so (``fetch_failed``). A setting naming no declared source is refused.
#
# Systems of record that extensions contribute (``Extension.systems``, ADR-P20 S2) are
# built here, once per full name (``<extension>.<name>``), by calling the spec's factory
# with ``state_dir``: a folder beside the SQLite file (``systems/<full name>``) where a
# local or fixture system may keep its own ledger. A real adapter may ignore it.
#
# ``AKSHA_SANDBOX`` is a declaration, not a probe: no sandbox backend is built yet
# (ADR-P12), so the operator names the one the process runs inside, for example
# ``bwrap:os-jail``. A headless start needs it (ADR-P11 D12); strength ``none`` does not
# count as a sandbox.

DEFAULT_AGENTS = Path(__file__).resolve().parent / "agents"
DEFAULT_EXTENSIONS = Path(__file__).resolve().parent / "extensions"
SERVER_FILE = "aksha.server.json"
"""Beside the store: the URL of the server holding the writer lock, written by ``serve``."""
SYSTEMS_DIR = "systems"
"""Beside the store: one state folder per contributed system of record."""


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
        )

    def sandbox_availability(self) -> Availability | None:
        """The declared sandbox (``AKSHA_SANDBOX``), or None when none is declared."""
        if self.sandbox is None:
            return None
        backend, _, strength = self.sandbox.partition(":")
        strength = strength or "os-jail"
        if not _is_strength(strength):
            raise StoreError(
                "sandbox_invalid", f"AKSHA_SANDBOX={self.sandbox!r}: strength is one of {STRENGTHS}"
            )
        usable = strength != "none"
        return Availability(backend, strength, usable,
                            "" if usable else "strength none is not a sandbox")  # fmt: skip

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
) -> SessionAgent:
    """One loaded definition bound to its models, as a session runs it."""
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
    if loaded is None:
        loaded = load_definitions(config, catalogue=catalogue)
    commit = runtime_commit()
    return tuple(
        session_agent(d, config.mode, (adapters or {}).get(d.agent.name), commit=commit)
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


def source_reader(config: RuntimeConfig, loaded: Sequence[LoadedDefinition]) -> FolderSources:
    """The record reader over every declared versioned source that has a folder.

    A source's folder is its ``AKSHA_SOURCES`` setting, else its spec's ``root``. Raises
    ``StoreError`` for a setting that names no declared source (``source_unknown``).
    """
    declared: dict[str, VersionedSourceSpec] = {}
    for definition in loaded:
        for spec in definition.agent.sources:
            declared.setdefault(spec.name, spec)
        for ext in definition.extensions:
            for spec in ext.ext.sources:
                declared.setdefault(qualify(ext.ext.name, spec.name), spec)
    unknown = sorted(set(config.sources) - set(declared))
    if unknown:
        raise StoreError("source_unknown", f"AKSHA_SOURCES names {unknown}, which no loaded "
                         f"agent declares; declared: {sorted(declared)}")  # fmt: skip
    folders: dict[str, Path] = {}
    for name, spec in declared.items():
        folder = config.sources.get(name) or (Path(spec.root) if spec.root else None)
        if folder is not None:
            folders[name] = folder
    return FolderSources(folders)


def open_session(
    config: RuntimeConfig,
    clock: Clock | None = None,
    *,
    adapters: Mapping[str, Mapping[str, object]] | None = None,
    systems: Mapping[str, SystemOfRecord] | None = None,
) -> Session:
    """The writer's session: store (writer lock), agents, router, sandbox, systems, reader.

    The systems are the extensions' (:func:`extension_systems`) with ``systems`` added
    over them by name (tests, scripted runs). Raises ``LockHeld`` when another process
    holds the writer lock (a server, or another embedded command).
    """
    clock = clock or SystemClock()
    catalogue = Catalogue(CORE_EVENT_TYPES)
    loaded = load_definitions(config, catalogue=catalogue)
    agents = load_session_agents(config, adapters, loaded=loaded)
    given = dict(systems or {})
    every_system = {**extension_systems(config, loaded, skip=given), **given}
    reader = source_reader(config, loaded)
    store = open_event_store(config.storage, clock, writer=True, catalogue=catalogue)
    ids = LogIdSource(store)
    router = None
    if len(agents) >= 2:
        routable = tuple(RoutableAgent(a.name, a.agent.purpose, a.definition_hash) for a in agents)
        router = Router(routable, PurposeClassifier(), ids)
    sandbox = config.sandbox_availability()
    return Session(store, clock, agents, router=router, sandbox=lambda: sandbox,
                   blobs=open_blob_store(config.storage), systems=every_system,
                   reader=reader, ids=ids)  # fmt: skip


def open_reader(config: RuntimeConfig, clock: Clock | None = None) -> EventStore:
    """A read-only handle on the configured store: never takes the lock, never boots."""
    return open_event_store(config.storage, clock or SystemClock(), writer=False)


def session_capabilities(session: Session, config: RuntimeConfig) -> CapabilityRecord:
    """The capability record of ``session`` (ADR-P19 D7) with this process's commit."""
    commit, dirty = runtime_commit()
    sandbox = config.sandbox_availability()
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
    patent_source: SharedFolder
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


class SharedFolder:
    """The one source folder both agents read, as one versioned source (ADR-P05 D6).

    The two agents name records of the same files differently: the research agent's
    folder reader by path (``priorities/business-priorities.md``), the patent agent's
    world by path and view (``notes/C1-self-calibration.md#note``). A record's version is
    answered by the reader that names it that way, so a change set checks every record
    either agent read against the reader it came from, and a file nobody's view uses
    moves nothing. Record IDs carry no source today, so one source per folder is what
    keeps the research agent's reads from reading as gone to the patent agent's world.
    """

    def __init__(self, world: SourceWorld, folder: FolderSource, collections: tuple[str, ...]):
        """Answer for ``world``'s view IDs and ``folder``'s path IDs over ``collections``."""
        self.world = world
        self.folder = folder
        self._collections = collections

    @property
    def name(self) -> str:
        """The source's name: the patent agent's, whose change set this records."""
        return self.world.name

    @property
    def collections(self) -> tuple[str, ...]:
        """Both agents' collections."""
        return self._collections

    def version(self, owner: str) -> str:
        """The whole folder's version."""
        return self.world.version(owner)

    def record_version(self, owner: str, record_id: str) -> str | None:
        """A view ID's version from the world, a path ID's from the folder reader."""
        if "#" in record_id:
            return self.world.record_version(owner, record_id)
        return self.folder.record_version(owner, record_id)

    def run_query(self, owner: str, query: str) -> str:
        """Re-execute a recorded publication search."""
        return self.world.run_query(owner, query)


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
    from aksha.agents.patent.world import COLLECTIONS as PATENT_COLLECTIONS
    from aksha.agents.patent.world import SourceWorld
    from aksha.agents.research.fakes import ResearchModel
    from aksha.agents.research.tools import COLLECTIONS as RESEARCH_COLLECTIONS
    from aksha.agents.research.tools import SOURCE as RESEARCH_SOURCE

    clock = FakeClock(DEMO_START)
    store = sqlite.open_store(work / "aksha.sqlite", clock, writer=True)
    blobs = FilesystemBlobStore(work / "blobs")
    loaded = {
        d.agent.name: d for d in load_agents([DEFAULT_AGENTS / n for n in DEMO_AGENT_NAMES], [])
    }
    research, patent = loaded["research"], loaded["patent"]
    folder = FolderSource(RESEARCH_SOURCE, sources)
    research_setup = _setup(
        store, research, research.agent, research.registry, ResearchModel(),
        requests[research.agent.name], blobs, folder,
    )  # fmt: skip
    world = SourceWorld(sources, DEMO_START.date())
    agent = build_patent_agent(world)
    listed = {t.spec.name: t for t in agent.tools if isinstance(t, ToolDecl)}
    patent_setup = _setup(
        store, patent, agent, ToolRegistry(agent, listed, ()), PatentFake(),
        requests[agent.name], blobs,
    )  # fmt: skip
    send_tools = {"send_packet": ApprovalTool(listed["send_packet"].spec)}
    hashes = {research.agent.name: research.definition_hash, agent.name: patent.definition_hash}
    authority = approval_authority(send_tools, lambda entry: patent.definition_hash)
    box = mailbox or counsel_mailbox(work)
    worker = OutboxWorker(store, DEMO_OWNER, clock, {COUNSEL_MAILBOX: box}, authority=authority)
    collections = tuple(dict.fromkeys((*PATENT_COLLECTIONS, *RESEARCH_COLLECTIONS)))
    shared = SharedFolder(world, folder, collections)
    return DemoRuntime(
        store, clock, research_setup, patent_setup, shared, send_tools, worker, box, hashes
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


def eval_agents() -> AgentRegistry:
    """The agents an eval can run, by the name cases list them under.

    The registration hook for the real agents: when an agent's eval adapter exists in
    ``aksha/evals/agents/<name>.py``, register it here, for example
    ``registry.register("research", ResearchUnderTest, mode="fake")``. A case naming an
    agent that is not registered is refused with ``agent_not_registered``.
    """
    registry = AgentRegistry()
    registry.register(
        "scripted",
        ScriptedResearchAgent,
        mode="fake",
        description="small rule-based research agent over competitor pages and reviews",
    )
    return registry


def eval_judge() -> Judge:
    """The fake judge for fake model mode: rule-based, offline, annotate-only."""
    return Judge(model=RuleJudge(), model_id="fake:RuleJudge")
