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
``AKSHA_HOME``         the folder local state lives in        the data folder below
``AKSHA_SQLITE_PATH``  the SQLite file                        ``<home>/aksha.sqlite``
``AKSHA_BLOB_PATH``    the filesystem blob store's folder     ``<home>/blobs``
=====================  =====================================  =====================

Without ``AKSHA_HOME``, local state goes to the platform's per-user data folder
(:func:`data_home`), so an installed ``aksha`` never scatters files into whatever folder
it was typed in. The ``make`` targets of a checkout set ``AKSHA_HOME=var``.

SQLite is the default so a run needs no setup: one file, no server (decision 34, 26 Nov
2026). PostgreSQL is the production backend (ADR-P01 D1): choose it with
``AKSHA_STORE=postgres`` and ``AKSHA_POSTGRES_DSN``. Both pass one contract suite. The
PostgreSQL driver is the optional extra ``aksha-harness[postgres]``, imported only when PostgreSQL
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
import random
import subprocess
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TypeGuard, cast
from urllib.parse import urlsplit

from aksha import data
from aksha.adapters.blobs.filesystem import FilesystemBlobStore
from aksha.adapters.clock.fake import FakeClock
from aksha.adapters.clock.system import SystemClock
from aksha.adapters.egress.network import HttpTransport, SocketResolver
from aksha.adapters.recording.models import Recorded, recorded_adapters, recording_adapters
from aksha.adapters.recording.sink import JsonlSink, Sink, read_stream
from aksha.adapters.recording.web import RecordingFetch, RecordingSearch, ReplayWeb
from aksha.adapters.sandbox import e2b
from aksha.adapters.sandbox.bwrap import BwrapSandbox
from aksha.adapters.search.world import WorldWeb, load_pages, world_hints
from aksha.adapters.sources.folder import FolderSource, FolderSources
from aksha.adapters.sources.workspace import InputWorkspace
from aksha.adapters.storage import sqlite
from aksha.adapters.systems.folder import FolderSystem
from aksha.adapters.systems.mailbox import MailboxSystem
from aksha.evals.agents.runtime import EpisodeSource, EpisodeWeb, RuntimeUnderTest
from aksha.evals.agents.scripted import ScriptedResearchAgent
from aksha.evals.case import Arm
from aksha.evals.judge import Judge, RuleJudge
from aksha.evals.ports import StoreOpener
from aksha.evals.registry import AgentRegistry
from aksha.extensions.ingest.types import FileInput
from aksha.extensions.ingest.worker import ParserPool
from aksha.runtime.artifacts.ports import BlobStore
from aksha.runtime.artifacts.store import ArtifactStore
from aksha.runtime.budgets.types import Backoff, Budget, Limits
from aksha.runtime.context.reentry import ReentrySetting
from aksha.runtime.context.types import effective_policy
from aksha.runtime.corrections.types import CorrectionRouting
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
from aksha.runtime.errors import AkshaError
from aksha.runtime.events.catalogue import CORE_EVENT_TYPES, Catalogue
from aksha.runtime.execution.errors import TransientStepError
from aksha.runtime.execution.executor import StepExecutor
from aksha.runtime.execution.ids import LogIdSource
from aksha.runtime.execution.ports import Clock
from aksha.runtime.grounding.ports import RecordReader
from aksha.runtime.interrupts.approvals import ApprovalTool, approval_authority
from aksha.runtime.invalidation.types import VersionedSourceSpec
from aksha.runtime.loop.types import LoopSetup
from aksha.runtime.models.calls import ModelCallFailed, ToolModelCalls
from aksha.runtime.models.errors import BindRefusal, BindRefused, PermanentModelError
from aksha.runtime.models.routing import (
    LIVE_ENV_PREFIX,
    AliasTable,
    bind_aliases,
    declared_aliases,
    import_class,
    live_adapters,
    live_env_name,
)
from aksha.runtime.models.routing import live_model_name as model_for
from aksha.runtime.models.types import (
    IMPLICIT_GENERATION_ALIAS,
    ModelAlias,
    ModelCapabilities,
    RunMode,
)
from aksha.runtime.outbox.ports import SystemOfRecord
from aksha.runtime.outbox.worker import OutboxWorker
from aksha.runtime.projections.view import load_view
from aksha.runtime.router.fake import PurposeClassifier
from aksha.runtime.router.router import Router
from aksha.runtime.sandbox.errors import SandboxRefused
from aksha.runtime.sandbox.ports import Sandbox, SessionWorkspaces
from aksha.runtime.sandbox.select import AUTO, select_backend
from aksha.runtime.sandbox.tier import ExecutionTier
from aksha.runtime.sandbox.types import (
    STRENGTHS,
    Availability,
    EnvironmentSpec,
    SessionRef,
    Strength,
)
from aksha.runtime.session.capabilities import CapabilityRecord, capabilities
from aksha.runtime.session.service import Session, SessionAgent
from aksha.runtime.storage.errors import StoreError
from aksha.runtime.storage.ports import EventStore
from aksha.runtime.tasks.tree import compile_tree
from aksha.runtime.timers.freshness import lifetimes
from aksha.runtime.tools.registry import ToolRegistry
from aksha.runtime.tools.types import ToolDecl
from aksha.runtime.web.ports import WebFetch, WebSearch
from aksha.runtime.web.types import WebError, web_record_id
from aksha.uploads import UploadIngester, profile_named

Backend = Literal["sqlite", "postgres"]


def data_home(
    env: Mapping[str, str], *, platform: str = sys.platform, home: Path | None = None
) -> Path:
    """Where local state lives: ``AKSHA_HOME``, else the platform's per-user data folder.

    Linux and others: ``$XDG_DATA_HOME/aksha`` (when absolute) or ``~/.local/share/aksha``;
    macOS: ``~/Library/Application Support/aksha``; Windows: ``%LOCALAPPDATA%/aksha``.
    """
    if env.get("AKSHA_HOME"):
        return Path(env["AKSHA_HOME"])
    base = home if home is not None else Path.home()
    if platform == "win32":
        local = env.get("LOCALAPPDATA")
        return (Path(local) if local else base / "AppData" / "Local") / "aksha"
    if platform == "darwin":
        return base / "Library" / "Application Support" / "aksha"
    xdg = env.get("XDG_DATA_HOME", "")
    return (Path(xdg) if xdg and Path(xdg).is_absolute() else base / ".local" / "share") / "aksha"


def _default_home() -> Path:
    return data_home({})


@dataclass(frozen=True)
class StorageConfig:
    """Which event store backend to open, and where."""

    backend: Backend = "sqlite"
    postgres_dsn: str | None = None
    sqlite_path: Path = field(default_factory=lambda: _default_home() / "aksha.sqlite")
    blob_path: Path = field(default_factory=lambda: _default_home() / "blobs")

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> StorageConfig:
        """Read the configuration from environment variables (see the module docstring)."""
        backend = env.get("AKSHA_STORE", "sqlite")
        if backend not in ("sqlite", "postgres"):
            raise StoreError("unknown_backend", f"AKSHA_STORE={backend!r}: use sqlite or postgres")
        home = data_home(env)
        return cls(
            backend=cast(Backend, backend),
            sqlite_path=Path(env.get("AKSHA_SQLITE_PATH") or home / "aksha.sqlite"),
            postgres_dsn=env.get("AKSHA_POSTGRES_DSN"),
            blob_path=Path(env.get("AKSHA_BLOB_PATH") or home / "blobs"),
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
#                          or ``live`` (a chat-completions server, below)
# ``AKSHA_LIVE_*``         live mode's server, key and models (below)      none
# ``AKSHA_SANDBOX``        the sandbox backend: ``bwrap``, ``e2b``,        none
#                          ``auto``, ``refuse``, ``none``, or
#                          ``<name>:<strength>``
# ``AKSHA_SANDBOX_PATH``   where session workspaces live (decision 37)     ``<home>/sandbox``
# ``AKSHA_WORKSPACE``      the folder or git repository a new session      none (empty)
#                          workspace starts from
# ``AKSHA_E2B_TEMPLATE``   the E2B template a session sandbox starts from  ``base``
# ``AKSHA_E2B_TIMEOUT_S``  an E2B sandbox's idle timeout, in seconds       ``900``
# ``AKSHA_E2B_ON_END``     at the idle timeout: ``pause`` or ``kill``       ``pause``
# ``AKSHA_NETWORK``        ``on`` gives tools the real network under       ``off``
#                          their egress policies (ADR-P26)
# ``AKSHA_APPROVED_ENVIRONMENTS``  environment keys an operator granted   none
#                          a first build (ADR-P28 D4), ``,``-separated
# ``AKSHA_SERVER_URL``     the server a CLI command talks to               the server file
# ``AKSHA_SOURCES``        ``<source>=<folder>`` pairs (``os.pathsep``)    each spec's ``root``
# ``AKSHA_REENTRY``        re-entry setting a request run records:         ``fresh``
#                          ``fresh`` or ``resume`` (decision 36)
# ``AKSHA_SYSTEMS``        ``<system>=<kind>:<folder>`` pairs (``,``)      none
# ``AKSHA_CORRECTIONS``    ``<source>=<system>`` pairs (``,``): where an   none
#                          approved correction to a shared source goes
# ``AKSHA_CORRECTION_APPROVER``  the domain owner's role, or              ``person``
#                          ``<source>=<role>`` pairs (``,``)
# ``AKSHA_REUSE``          ``off`` makes every run recompute instead of    ``on``
#                          reusing an earlier run's work (ADR-P04 R1)
# ``AKSHA_IMAGE_PROFILE``  the image profile of the model reading uploads  none (no vision)
#                          (``patch28-standard``, ``patch28-hires``,
#                          ``patch32-high``, ``flat-1120``)
# ``AKSHA_SEARCH``         the web ``web_search`` and ``fetch_url`` reach: none (``web_off``)
#                          ``world`` (a world's offline web), ``brave``
#                          (live search, plain HTTP fetch)
# ``AKSHA_WEB_WORLD``      the world ``AKSHA_SEARCH=world`` serves: a      none
#                          bundled world's name or a world folder
# ``AKSHA_BRAVE_API_KEY``  the live search key (never recorded)            none
# ``AKSHA_RECORD``         a folder every model call, search and fetch     none
#                          is appended to (``aksha replay`` reads it)
# =======================  ==============================================  ==================
#
# ``AKSHA_SEARCH`` picks the web ports (ADR-P26 A3). ``world`` (or ``world:<name|folder>``)
# serves a world's ``web.json``: its search proxy ranks the world's pages, noise mixed in,
# the same way for a query every time, and a fetch returns the page's file; an offline
# model of an agent is told the world's hints for it (``with_world_hints``). ``brave``
# searches the Brave Search API with ``AKSHA_BRAVE_API_KEY`` (``search_key_missing``
# without it) and fetches each page over HTTP through the fetch tool's egress policy
# (``aksha.web``), so it needs ``AKSHA_NETWORK=on`` (``network_off`` otherwise). Unset,
# the web tools answer ``web_off``. ``AKSHA_RECORD`` wraps every model adapter and both web
# ports so each answer is appended to the folder (``adapters.recording``); ``aksha run
# --record`` sets it for one run in a store of its own (:func:`record_run`).
#
# ``AKSHA_SOURCES`` names the folder each declared versioned source reads (ADR-P13 B1),
# for example ``inputs=src/aksha/data/worlds/<world>/sources`` for the research and patent
# agents, which both read the ``inputs`` source. Uploads (``aksha run --input``, ``aksha
# upload``, ``UploadInputs``) go to each owner's input workspace beside the SQLite file
# (``inputs/<owner>/``), which every run of that owner reads over the configured folder. A
# declared source with neither a setting nor a ``root`` has no reader: its tools read
# nothing and say so (``fetch_failed``). A setting naming no declared source is refused.
#
# Every upload goes through the ingest extension (:mod:`aksha.uploads`): its bytes are
# stored in the blob store and typed from content, and a PDF, an Office file, an email, an
# image or text in another charset reaches the agent as a header that says what was done
# followed by its text, with each email attachment as its own input. The parsers are the
# optional extra ``aksha-harness[ingest]``; without it those types are named gaps.
# ``AKSHA_IMAGE_PROFILE`` sizes images for the reading model; unset, an image is its
# header alone ("no vision-capable model configured").
#
# Systems of record that extensions contribute (``Extension.systems``, ADR-P20 S2) are
# built here, once per full name (``<extension>.<name>``), by calling the spec's factory
# with ``state_dir``: a folder beside the SQLite file (``systems/<full name>``) where a
# local or fixture system may keep its own ledger. A real adapter may ignore it.
#
# ``AKSHA_CORRECTIONS`` routes approved corrections (ADR-P05 C1): a correction to an
# owner's own upload becomes that file's next version; one to a record of a shared source
# (``AKSHA_SOURCES``) goes through the outbox to the system named here, which must be bound.
# ``AKSHA_CORRECTION_APPROVER`` names the role that decides: a command sent under another
# role (``by``) is refused ``not_domain_owner``.
#
# ``AKSHA_SYSTEMS`` is opt-in and for development only. It binds a system of record that
# an effect names (``counsel_mail`` for the patent agent's ``send_packet``) or that an
# extension contributes (``<extension>.<name>``, replacing the extension's own) to a mock
# kind the composition root builds. The one kind is ``mailbox``: the ledgered mock mailbox
# ``make demo`` uses (``adapters.systems.mailbox``), a JSON file ``<folder>/mailbox.json``
# that delivers once per idempotency key and reaches no one. The kind ``folder`` writes each
# approved correction's text as a file into ``<folder>`` (``adapters.systems.folder``), a
# shared source's folder, with its ledger beside the store. Nothing is bound by default,
# so an approval whose effect has no system is refused ``system_unavailable``; a name no
# loaded agent or extension uses is refused ``system_unknown``, an unknown kind
# ``system_invalid``. The CLI says on every command that the bound system is a mock.
#
# ``AKSHA_SANDBOX`` chooses tier 3's backend once, at start, from side-effect-free probes
# (ADR-P12 decision 2). ``bwrap`` (or ``bwrap:os-jail``) is the bubblewrap backend,
# ``e2b`` (or ``e2b:vm``) the E2B cloud backend, which needs ``E2B_API_KEY`` in the
# environment, ``auto`` the strongest local built-in backend that probes available (never
# a cloud one), ``refuse`` the backend that runs nothing, and ``none`` the same as unset.
# A built-in backend that does not probe available becomes ``refuse``
# with the probe's reason, never a weaker box. ``<name>:<strength>`` with any other name
# declares an outer box the operator attests the whole process runs in (a container,
# a VM): it lets a headless run start (ADR-P11 D12), and it runs no program, so an exec
# tool is still refused ``sandbox_unavailable``. Unset, no backend is chosen. Strength
# ``none`` is never a sandbox. The choice, every probe and the network setting are
# recorded on each ``run.started`` (decision 9).
#
# Decision 37: one stateful sandbox per session. The bubblewrap backend binds a folder per
# session under ``AKSHA_SANDBOX_PATH`` (``bwrap/<owner>/<session>/work``) as ``/work``, so
# every exec of the session's runs sees what the earlier ones wrote; the E2B backend keeps
# one remote sandbox per session by its ID (``e2b/<owner>/<session>/e2b.json``). A new
# bubblewrap workspace starts from ``AKSHA_WORKSPACE``: a git repository becomes a git
# worktree on the branch ``aksha/<owner>/<session>`` (never the checkout itself), any other folder
# is copied in, and unset is an empty folder. Workspaces are kept after the run;
# ``aksha sandbox clean <session>`` removes one (:func:`clean_sandbox_session`).
#
# ``AKSHA_MODE=live`` binds every alias of every agent to a live adapter (ADR-P07 D9 as
# amended G2 and G6): ``AKSHA_ALIAS_<NAME>=module:Class`` names one, else the built-in
# adapter of the alias's class under ``AKSHA_LIVE_PROVIDER`` (``AKSHA_LIVE_PROVIDER_<CLASS>``
# for one class). The default provider is ``litellm`` (``adapters.models.litellm``, the
# extra ``aksha-harness[live]``): one adapter per class over LiteLLM, which reaches every
# provider it supports, the model named in LiteLLM's form (``gemini/gemini-4-neon``). Its
# keys are the providers' own variables (``GEMINI_API_KEY``, ...), which LiteLLM reads from
# the environment: Aksha never takes, passes or records them. ``chat-completions`` is the
# standard-library fallback (``adapters.models.live``) over ``AKSHA_LIVE_BASE_URL`` (any
# server speaking the chat-completions shape) with ``AKSHA_LIVE_API_KEY``. The model is
# ``AKSHA_LIVE_MODEL_<ALIAS>``, else ``AKSHA_LIVE_MODEL_<CLASS>`` (``GENERATION``,
# ``EXTRACTION``, ``CLASSIFICATION``), else ``AKSHA_LIVE_MODEL``. The start is refused,
# before any call, with ``live_config_missing`` (no model for an alias, an unknown
# provider, LiteLLM not installed, or no server for ``chat-completions``) or
# ``live_key_missing`` (the provider variables LiteLLM says the model needs are unset,
# named; or no ``AKSHA_LIVE_API_KEY`` for a chat-completions server not on this machine).
# ``run.started`` records each adapter's class, never a setting.
#
# A live model is slower than a fake, so live mode sizes the agent's limits up (and
# ``run.started{budget}`` records the active-time cap): the active-time cap becomes
# ``AKSHA_LIVE_ACTIVE_MS`` when set, else at least :data:`LIVE_ACTIVE_MS` (an hour), and the
# model step's timeout (``Limits.model_timeout_ms``) at least the generation adapter's
# attempt timeout (``AKSHA_LIVE_TIMEOUT_S``) plus :data:`LIVE_TIMEOUT_MARGIN_MS`. A real
# provider sheds load (a 429, a 503 "high demand", a timeout) for seconds to minutes, so
# the model step's retries become ``AKSHA_LIVE_RETRIES`` (default :data:`LIVE_RETRIES`, or
# the agent's own if higher) with :data:`LIVE_BACKOFF`: from ``AKSHA_LIVE_BACKOFF_MS``
# (2 s) doubling to ``AKSHA_LIVE_BACKOFF_CAP_MS`` (60 s), a quarter of each delay drawn at
# random, at least the provider's ``Retry-After``, and never past the run's active time
# (ADR-P14 B2). A live model also takes more turns than a scripted fake to reach the same
# save (it misreads a schema, fixes a refused cite), so every Task's turn limit and the
# run's turn and tool-call budget are multiplied by ``AKSHA_LIVE_TURN_FACTOR`` (a whole
# number from 1, default :data:`LIVE_TURN_FACTOR`). ``run.started{turn_factor}`` records it
# (the stop policy reads it there) and a recording's manifest keeps it, so a resumed run and
# a replay run under the same limits (:func:`scaled_limits`). Fake mode, and so every
# offline eval, keeps the agent's own limits (:func:`live_limits`). A recording's manifest
# also keeps the active-time cap, model step timeout, retries and backoff each agent ran
# under (``limits``, :func:`run_limits`), and a replay runs under those, never under the
# replaying machine's ``AKSHA_LIVE_*`` (it reads none).
#
# ``AKSHA_NATIVE_FILES`` is ``off`` unless set to ``on``: off, no file is sent natively and
# every upload goes through intake's parsers, its route recorded ``text`` with the reason
# (:func:`native_file_input`).
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
MOCK_SYSTEM_KINDS = ("mailbox", "folder")
"""The development systems of record ``AKSHA_SYSTEMS`` can bind (mocks, never real ones)."""


@dataclass(frozen=True)
class SandboxSettings:
    """The session workspaces' settings (decision 37; the table above)."""

    root: Path = field(default_factory=lambda: _default_home() / "sandbox")
    workspace: Path | None = None
    e2b_template: str = e2b.TEMPLATE
    e2b_timeout_s: int = 900
    e2b_on_end: str = "pause"
    e2b_api_url: str = e2b.API_URL
    e2b_envd_url: str = e2b.ENVD_URL

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SandboxSettings:
        """Read the settings; a bad value is refused ``sandbox_invalid``."""
        raw_timeout = env.get("AKSHA_E2B_TIMEOUT_S") or "900"
        if not raw_timeout.isdigit() or int(raw_timeout) <= 0:
            why = f"AKSHA_E2B_TIMEOUT_S={raw_timeout!r}: a positive number of seconds"
            raise StoreError("sandbox_invalid", why)
        on_end = env.get("AKSHA_E2B_ON_END") or "pause"
        if on_end not in ("pause", "kill"):
            raise StoreError("sandbox_invalid", f"AKSHA_E2B_ON_END={on_end!r}: pause or kill")
        workspace = env.get("AKSHA_WORKSPACE")
        return cls(
            root=Path(env.get("AKSHA_SANDBOX_PATH") or data_home(env) / "sandbox"),
            workspace=Path(workspace).resolve() if workspace else None,
            e2b_template=env.get("AKSHA_E2B_TEMPLATE") or e2b.TEMPLATE,
            e2b_timeout_s=int(raw_timeout),
            e2b_on_end=on_end,
            e2b_api_url=env.get("AKSHA_E2B_API_URL") or e2b.API_URL,
            e2b_envd_url=env.get("AKSHA_E2B_ENVD_URL") or e2b.ENVD_URL,
        )


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
    sandboxes: SandboxSettings = field(default_factory=lambda: SandboxSettings())
    """Where session workspaces live and what they start from (decision 37)."""
    live_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    """``AKSHA_LIVE_*`` and ``AKSHA_ALIAS_*``: live mode's settings, the key among them.
    Read only when ``mode`` is ``live``, and never recorded."""
    image_profile: str | None = None
    """``AKSHA_IMAGE_PROFILE``: the image profile of the model that reads uploads, or None."""
    search: str | None = None
    """``AKSHA_SEARCH``: which web the web tools reach (the table above); None for none."""
    web_world: str | None = None
    """``AKSHA_WEB_WORLD``: the world ``AKSHA_SEARCH=world`` serves."""
    search_env: Mapping[str, str] = field(default_factory=dict, repr=False)
    """``AKSHA_BRAVE_*``: the live search provider's settings, the key among them. Never
    recorded."""
    record: Path | None = None
    """``AKSHA_RECORD``: the folder every model call, search and fetch is recorded into."""
    corrections: Mapping[str, str] = field(default_factory=dict)
    """``AKSHA_CORRECTIONS``: the system of record each shared source's corrections go to."""
    approver: str = "person"
    """``AKSHA_CORRECTION_APPROVER``: the domain owner's role for every source."""
    approvers: Mapping[str, str] = field(default_factory=dict)
    """``AKSHA_CORRECTION_APPROVER`` pairs: a source's own domain owner role."""
    reuse: str = "on"
    """``AKSHA_REUSE``: ``off`` records ``reuse: off`` on every run (ADR-P04 R1)."""
    turn_factors: Mapping[str, int] = field(default_factory=dict)
    """The turn factor each agent runs under, by name, where it is not its mode's own: a
    replay sets the ones its recording ran under (:func:`scaled_limits`). Not an
    environment setting."""
    run_limits: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """The active-time cap, model step timeout, retries and backoff each agent runs under, by
    name, in place of its mode's own: a replay sets the ones its recording ran under
    (:func:`run_limits`, :func:`with_run_limits`). Not an environment setting."""

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
            sandboxes=SandboxSettings.from_env(env),
            server_url=env.get("AKSHA_SERVER_URL") or None,
            sources=_source_settings(env.get("AKSHA_SOURCES", "")),
            reentry=env.get("AKSHA_REENTRY") or "fresh",
            systems=_system_settings(env.get("AKSHA_SYSTEMS", "")),
            network=env.get("AKSHA_NETWORK") or "off",
            image_profile=env.get("AKSHA_IMAGE_PROFILE") or None,
            approved_environments=tuple(
                k.strip()
                for k in env.get("AKSHA_APPROVED_ENVIRONMENTS", "").split(",")
                if k.strip()
            ),
            live_env={
                k: v
                for k, v in env.items()
                if k.startswith(("AKSHA_LIVE_", LIVE_ENV_PREFIX)) or k == "AKSHA_NATIVE_FILES"
                if v
            },
            search=env.get("AKSHA_SEARCH") or None,
            web_world=env.get("AKSHA_WEB_WORLD") or None,
            search_env={k: v for k, v in env.items() if k.startswith("AKSHA_BRAVE_") and v},
            record=Path(env["AKSHA_RECORD"]) if env.get("AKSHA_RECORD") else None,
            corrections=_pairs("AKSHA_CORRECTIONS", env.get("AKSHA_CORRECTIONS", "")),
            approver=_approver(env.get("AKSHA_CORRECTION_APPROVER", "")),
            approvers=_pairs(
                "AKSHA_CORRECTION_APPROVER", _role_pairs(env.get("AKSHA_CORRECTION_APPROVER", ""))
            ),
            reuse=_reuse(env.get("AKSHA_REUSE") or "on"),
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


def _pairs(name: str, raw: str) -> dict[str, str]:
    """``a=b`` items separated by commas."""
    out: dict[str, str] = {}
    for item in (part.strip() for part in raw.split(",") if part.strip()):
        key, sep, value = item.partition("=")
        if not sep or not key.strip() or not value.strip():
            raise StoreError("setting_invalid", f"{name} item {item!r}: use name=value")
        out[key.strip()] = value.strip()
    return out


def _role_pairs(raw: str) -> str:
    """The ``<source>=<role>`` items of ``AKSHA_CORRECTION_APPROVER``."""
    return ",".join(p for p in raw.split(",") if "=" in p)


def _approver(raw: str) -> str:
    """The plain role among ``AKSHA_CORRECTION_APPROVER``'s items, ``person`` without one."""
    plain = [p.strip() for p in raw.split(",") if p.strip() and "=" not in p]
    return plain[-1] if plain else "person"


def _reuse(raw: str) -> str:
    if raw not in ("on", "off"):
        raise StoreError("setting_invalid", f"AKSHA_REUSE={raw!r}: use on or off")
    return raw


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
        f"mock: {name} is a development {kind} at "
        f"{folder if kind == 'folder' else folder / 'mailbox.json'} (AKSHA_SYSTEMS); "
        "nothing it receives leaves this machine"
        for name, (kind, folder) in sorted(config.systems.items())
    )


def _is_strength(value: str) -> TypeGuard[Strength]:
    return value in STRENGTHS


def runtime_commit(root: Path | None = None) -> tuple[str, bool]:
    """The git commit of the running code and whether its tree is dirty, when readable.

    Only a checkout of this project has one: ``root`` (the folder above ``src/``) must hold
    both ``.git`` and ``src/aksha``. Installed from a wheel, the folder above the package
    may belong to some other repository, so the answer is ``("unknown", False)``.
    """
    root = root or Path(__file__).resolve().parents[2]
    if not ((root / ".git").exists() and (root / "src" / "aksha").is_dir()):
        return "unknown", False
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


LIVE_ADAPTERS: Mapping[str, str] = {
    "generation": "aksha.adapters.models.live:LiveGenerationModel",
    "extraction": "aksha.adapters.models.live:LiveExtractionModel",
    "classification": "aksha.adapters.models.live:LiveClassificationModel",
}
"""The built-in live adapter of each model class, imported only in live mode."""

LITELLM_ADAPTERS: Mapping[str, str] = {
    "generation": "aksha.adapters.models.litellm:LiteLLMGenerationModel",
    "extraction": "aksha.adapters.models.litellm:LiteLLMExtractionModel",
    "classification": "aksha.adapters.models.litellm:LiteLLMClassificationModel",
}
"""The LiteLLM adapter of each model class (``AKSHA_LIVE_PROVIDER=litellm``, ADR-P07 G6)."""

LIVE_PROVIDERS: Mapping[str, Mapping[str, str]] = {
    "litellm": LITELLM_ADAPTERS,
    "chat-completions": LIVE_ADAPTERS,
}
"""``AKSHA_LIVE_PROVIDER`` (or ``AKSHA_LIVE_PROVIDER_<CLASS>``): whose built-in adapters."""

DEFAULT_LIVE_PROVIDER = "litellm"

LIVE_ACTIVE_MS = 3_600_000
"""Live mode's least active-time cap (an hour), unless ``AKSHA_LIVE_ACTIVE_MS`` says."""

LIVE_TIMEOUT_MARGIN_MS = 15_000
"""How much longer a live model step waits than its adapter's own attempt timeout."""

LIVE_RETRIES = 6
"""Live mode's least retries of a model step, unless ``AKSHA_LIVE_RETRIES`` says."""

LIVE_BACKOFF = Backoff(base_ms=2_000, factor=2, cap_ms=60_000)
"""Live mode's model-step backoff: 2, 4, 8, 16, 32, 60, 60 s..."""

LIVE_JITTER = 0.25
"""The share of each live backoff drawn at random, so runs that failed together spread out."""

LIVE_TURN_FACTOR = 3
"""Live mode's multiplier on turn and tool-call limits, unless ``AKSHA_LIVE_TURN_FACTOR`` says."""


def live_turn_factor(env: Mapping[str, str]) -> int:
    """``AKSHA_LIVE_TURN_FACTOR``, else :data:`LIVE_TURN_FACTOR` (app comment above).

    Raises ``BindRefused`` (``live_config_missing``) unless it is a whole number from 1.
    """
    raw = (env.get("AKSHA_LIVE_TURN_FACTOR") or "").strip()
    if not raw:
        return LIVE_TURN_FACTOR
    if not raw.isdigit() or int(raw) < 1:
        message = f"AKSHA_LIVE_TURN_FACTOR={raw!r}: a whole number from 1"
        raise BindRefused((BindRefusal("*", "live_config_missing", message),))
    return int(raw)


def scaled_limits(agent: Agent, factor: int) -> Agent:
    """``agent`` with its run budget's turn and tool-call caps multiplied by ``factor``.

    The Task turn limits scale where they are read: the stop policy multiplies each by the
    ``turn_factor`` its run's ``run.started`` records (``budgets.limits.turn_factor``), so a
    resumed run keeps the factor it started under. Only the counts a slower model spends
    more of scale: the veto, restart and retry limits and the time caps keep their values.
    The agent itself when ``factor`` is 1.
    """
    if factor == 1:
        return agent
    budget = agent.budget or Budget()
    scaled = replace(
        budget, model_turns=budget.model_turns * factor, tool_calls=budget.tool_calls * factor
    )
    return replace(agent, budget=scaled)


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
"""Hosts a live server may be on without a key: this machine."""


def live_provider(model_class: str, env: Mapping[str, str]) -> str:
    """The provider of a class's built-in adapter: per class, else for all, else LiteLLM."""
    return (
        (
            env.get(f"AKSHA_LIVE_PROVIDER_{model_class.upper()}")
            or env.get("AKSHA_LIVE_PROVIDER")
            or DEFAULT_LIVE_PROVIDER
        )
        .strip()
        .lower()
    )


def live_defaults(env: Mapping[str, str]) -> dict[str, str]:
    """Each class's built-in live adapter under the configured providers (known ones only)."""
    out: dict[str, str] = {}
    for model_class in ("generation", "extraction", "classification"):
        table = LIVE_PROVIDERS.get(live_provider(model_class, env))
        if table is not None:
            out[model_class] = table[model_class]
    return out


def live_refusals(loaded: LoadedDefinition, env: Mapping[str, str]) -> list[BindRefusal]:
    """Why live mode cannot start for ``loaded`` with ``env`` (app comment above)."""
    out: list[BindRefusal] = []
    custom = {
        a for a in declared_aliases(loaded.model_aliases.values()) if env.get(live_env_name(a))
    }
    builtin = [a for a in declared_aliases(loaded.model_aliases.values()).values()
               if a.name not in custom]  # fmt: skip
    if not builtin:
        return out
    for model_class in sorted({a.model_class for a in builtin}):
        provider = live_provider(model_class, env)
        if provider not in LIVE_PROVIDERS:
            message = f"AKSHA_LIVE_PROVIDER is one of {sorted(LIVE_PROVIDERS)}, not {provider!r}"
            out.append(BindRefusal("*", "live_config_missing", message))
    chat = [a for a in builtin if live_provider(a.model_class, env) == "chat-completions"]
    lite = [a for a in builtin if live_provider(a.model_class, env) == "litellm"]
    out.extend(litellm_refusals(lite, env))
    if not chat:
        return out
    base = env.get("AKSHA_LIVE_BASE_URL", "")
    if not base:
        out.append(BindRefusal("*", "live_config_missing", "set AKSHA_LIVE_BASE_URL"))
    for a in chat:
        if not model_for(a.name, a.model_class, env):
            var = "AKSHA_LIVE_MODEL_" + "".join(c if c.isalnum() else "_" for c in a.name).upper()
            message = f"set AKSHA_LIVE_MODEL, AKSHA_LIVE_MODEL_{a.model_class.upper()} or {var}"
            out.append(BindRefusal(a.name, "live_config_missing", message))
    host = (urlsplit(base).hostname or "") if base else ""
    if base and not env.get("AKSHA_LIVE_API_KEY") and host not in LOCAL_HOSTS:
        message = f"set AKSHA_LIVE_API_KEY: {host or base!r} is not on this machine"
        out.append(BindRefusal("*", "live_key_missing", message))
    return out


def _need_model(a: ModelAlias) -> BindRefusal:
    var = "AKSHA_LIVE_MODEL_" + "".join(c if c.isalnum() else "_" for c in a.name).upper()
    message = f"set AKSHA_LIVE_MODEL, AKSHA_LIVE_MODEL_{a.model_class.upper()} or {var}"
    return BindRefusal(a.name, "live_config_missing", message)


def litellm_refusals(aliases: Sequence[ModelAlias], env: Mapping[str, str]) -> list[BindRefusal]:
    """Why the LiteLLM adapters cannot serve ``aliases``: no model, no LiteLLM, or no key.

    LiteLLM is imported here, in live mode only, to ask which provider variables each
    model needs (``validate_environment``); the refusal names them, never a value.
    """
    if not aliases:
        return []
    out: list[BindRefusal] = []
    models: dict[str, ModelAlias] = {}
    for a in aliases:
        model = model_for(a.name, a.model_class, env)
        if not model:
            out.append(_need_model(a))
        else:
            models.setdefault(model, a)
    if not models:
        return out
    from aksha.adapters.models import litellm as lite

    try:
        module = lite.load_litellm()
    except PermanentModelError as e:
        return [*out, BindRefusal("*", e.code, e.message)]
    if env.get("AKSHA_LIVE_BASE_URL"):
        return out  # a server of the operator's own: LiteLLM's key rules are not its rules
    for model, a in sorted(models.items()):
        missing = lite.missing_keys(model, module)
        if missing:
            names = ", ".join(missing)
            message = f"LiteLLM finds no key for {model}; it reads {names} (never recorded)"
            out.append(BindRefusal(a.name, "live_key_missing", message))
    return out


def live_limits(loaded: LoadedDefinition, env: Mapping[str, str]) -> Agent:
    """The agent with live mode's active-time cap, model step timeout and retries (comment above).

    Raises ``BindRefused`` (``live_config_missing``) for a malformed ``AKSHA_LIVE_ACTIVE_MS``,
    ``AKSHA_LIVE_TIMEOUT_S``, ``AKSHA_LIVE_RETRIES``, ``AKSHA_LIVE_BACKOFF_MS`` or
    ``AKSHA_LIVE_BACKOFF_CAP_MS``.
    """
    raw = _whole_number(env, "AKSHA_LIVE_ACTIVE_MS", "milliseconds")
    agent = loaded.agent
    budget = agent.budget or Budget()
    active = raw if raw is not None else max(budget.active_ms, LIVE_ACTIVE_MS)
    from aksha.adapters.models.litellm import timeout_s

    generation = [a for a in declared_aliases(loaded.model_aliases.values()).values()
                  if a.model_class == "generation"]  # fmt: skip
    try:
        seconds = max(timeout_s(a.name, a.model_class, env) for a in generation)
    except PermanentModelError as e:
        raise BindRefused((BindRefusal("*", e.code, e.message),)) from None
    limits = agent.limits or Limits()
    timeout_ms = max(limits.model_timeout_ms, int(seconds * 1000) + LIVE_TIMEOUT_MARGIN_MS)
    retries = _whole_number(env, "AKSHA_LIVE_RETRIES", "a count of retries")
    base = _whole_number(env, "AKSHA_LIVE_BACKOFF_MS", "milliseconds")
    cap = _whole_number(env, "AKSHA_LIVE_BACKOFF_CAP_MS", "milliseconds")
    base = LIVE_BACKOFF.base_ms if base is None else base
    cap = max(base, LIVE_BACKOFF.cap_ms if cap is None else cap)
    return replace(
        agent,
        budget=replace(budget, active_ms=active),
        limits=replace(
            limits,
            model_timeout_ms=timeout_ms,
            max_retries=max(limits.max_retries, LIVE_RETRIES) if retries is None else retries,
            backoff=replace(LIVE_BACKOFF, base_ms=base, cap_ms=cap),
            jitter=LIVE_JITTER,
            jitter_source=random.SystemRandom().random,
        ),
    )


def run_limits(agent: Agent) -> dict[str, Any]:
    """What of ``agent``'s limits live mode sets, as a recording keeps them (record and replay).

    The active-time cap, the model step timeout, its retries and backoff: they decide how
    often a failed call is asked again and how long a run may wait, so a replay that ran
    under the replaying machine's own would retry a recorded timeout fewer times, or stop
    sooner, than the run it replays. The turn and tool-call caps travel as ``turn_factors``.
    """
    budget = agent.budget or Budget()
    limits = agent.limits or Limits()
    b = limits.backoff
    return {"active_ms": budget.active_ms, "model_timeout_ms": limits.model_timeout_ms,
            "max_retries": limits.max_retries,
            "backoff": {"base_ms": b.base_ms, "factor": b.factor, "cap_ms": b.cap_ms}}  # fmt: skip


def with_run_limits(agent: Agent, recorded: Mapping[str, Any]) -> Agent:
    """``agent`` under the :func:`run_limits` a recording kept.

    Jitter is off: a replay draws no randomness, so each backoff is its full delay.
    """
    budget = agent.budget or Budget()
    limits = agent.limits or Limits()
    b = recorded.get("backoff") or {}
    backoff = Backoff(base_ms=int(b.get("base_ms", limits.backoff.base_ms)),
                      factor=int(b.get("factor", limits.backoff.factor)),
                      cap_ms=int(b.get("cap_ms", limits.backoff.cap_ms)))  # fmt: skip
    return replace(
        agent,
        budget=replace(budget, active_ms=int(recorded.get("active_ms", budget.active_ms))),
        limits=replace(
            limits,
            model_timeout_ms=int(recorded.get("model_timeout_ms", limits.model_timeout_ms)),
            max_retries=int(recorded.get("max_retries", limits.max_retries)),
            backoff=backoff,
            jitter=0.0,
            jitter_source=None,
        ),
    )


def _whole_number(env: Mapping[str, str], name: str, unit: str) -> int | None:
    """``env[name]`` as a whole number, None when unset; ``live_config_missing`` if malformed."""
    raw = (env.get(name) or "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        message = f"{name}={raw!r}: {unit}, a whole number"
        raise BindRefused((BindRefusal("*", "live_config_missing", message),))
    return int(raw)


def bind_live(loaded: LoadedDefinition, env: Mapping[str, str]) -> Mapping[str, object]:
    """Every alias's live adapter (app comment above); raises ``BindRefused`` first if unset."""
    refusals = live_refusals(loaded, env)
    if refusals:
        raise BindRefused(tuple(refusals))
    try:
        return live_adapters(loaded.model_aliases.values(), env, defaults=live_defaults(env))
    except PermanentModelError as e:  # a malformed setting an adapter read (never a value)
        raise BindRefused((BindRefusal("*", e.code, e.message),)) from None


def session_agent(
    loaded: LoadedDefinition,
    mode: str,
    adapters: Mapping[str, object] | None = None,
    *,
    commit: tuple[str, bool] = ("unknown", False),
    reentry: ReentrySetting | None = None,
    live_env: Mapping[str, str] | None = None,
    turn_factor: int | None = None,
    limits: Mapping[str, Any] | None = None,
) -> SessionAgent:
    """One loaded definition bound to its models, as a session runs it.

    In ``live`` mode with no ``adapters`` given, each alias is bound by :func:`bind_live`
    from ``live_env`` (the ``AKSHA_LIVE_*`` and ``AKSHA_ALIAS_*`` settings).

    ``reentry`` is the setting its request runs record (decision 36); None is ``fresh``.
    ``turn_factor`` scales the agent's turn and tool-call limits (:func:`scaled_limits`);
    None is live mode's :func:`live_turn_factor` and 1 in every other mode. A replay passes
    the factor its recording ran under. ``limits`` (:func:`run_limits`) replace the
    active-time cap, model step timeout, retries and backoff the mode would set: a replay
    passes the ones its recording ran under.
    """
    agent = loaded.agent
    if limits is not None:
        agent = with_run_limits(agent, limits)
    elif mode == "live":
        agent = live_limits(loaded, live_env or {})
    factor = turn_factor or (live_turn_factor(live_env or {}) if mode == "live" else 1)
    agent = scaled_limits(agent, factor)
    if adapters is None:
        if mode == "live":
            adapters = bind_live(loaded, live_env or {})
        elif mode != "fake":
            message = f"AKSHA_MODE={mode!r}: use fake or live"
            raise BindRefused((BindRefusal("*", "mode_unsupported", message),))
        else:
            adapters = _fake_adapters(loaded)
    table = bind_aliases(loaded.model_aliases.values(), cast(RunMode, mode), adapters)
    briefs = {
        path: (loaded.folder / node.task.brief).read_text(encoding="utf-8")
        for path, node in compile_tree(agent.name, agent.tasks).nodes.items()
        if node.task.brief
    }
    extra = dict(run_started_fields(loaded, runtime_commit=commit[0], dirty=commit[1]))
    if factor != 1:
        extra["turn_factor"] = factor
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
        lifetimes=lifetimes(
            (*agent.sources, *(spec for ext in loaded.extensions for spec in ext.ext.sources))
        ),
    )


def world_hinted(adapters: Mapping[str, object], hints: Mapping[str, Any]) -> dict[str, object]:
    """Each offline adapter that takes a world's hints (``for_world``) given ``hints``.

    A world may tell an agent's offline model what a person who knows its market would
    know (``web.json`` ``fake``: the searches to make). The model reads them through its
    own ``for_world``; an adapter without one, and every live adapter, is unchanged.
    """
    out: dict[str, object] = {}
    for alias, adapter in adapters.items():
        hook = getattr(adapter, "for_world", None)
        out[alias] = hook(hints) if callable(hook) and hints else adapter
    return out


def with_world_hints(bound: SessionAgent, hints: Mapping[str, Any]) -> SessionAgent:
    """``bound`` with its offline adapters given a world's ``hints`` (:func:`world_hinted`)."""
    if not hints or bound.table.mode != "fake":
        return bound
    table = bound.table
    adapters = world_hinted(table.adapters, hints)
    return replace(bound, table=bind_aliases(table.aliases.values(), table.mode, adapters))


def recorded_agent(bound: SessionAgent, sink: Sink) -> SessionAgent:
    """``bound`` with every model adapter recording its answers into ``sink`` (ADR-P22 A1)."""
    table = bound.table
    adapters = recording_adapters(table.adapters, sink)
    return replace(bound, table=bind_aliases(table.aliases.values(), table.mode, adapters))


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
            d,
            config.mode,
            (adapters or {}).get(d.agent.name),
            commit=commit,
            reentry=reentry,
            live_env=config.live_env,
            turn_factor=config.turn_factors.get(d.agent.name),
            limits=config.run_limits.get(d.agent.name),
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
    known = effect_system_names(loaded) | frozenset(config.corrections.values())
    unknown = sorted(set(config.systems) - known)
    if unknown:
        raise StoreError("system_unknown", f"AKSHA_SYSTEMS names {unknown}, which no loaded "
                         f"agent or extension uses; used: {sorted(known)}")  # fmt: skip
    out: dict[str, SystemOfRecord] = {}
    for name, (kind, folder) in config.systems.items():
        if kind == "folder":
            out[name] = FolderSystem(folder, name, config.system_dir(name) / "ledger.json")
        else:
            out[name] = MailboxSystem(folder / "mailbox.json", name)
    return out


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


def upload_ingester(config: RuntimeConfig) -> UploadIngester:
    """The ingester every upload goes through, sizing images for ``AKSHA_IMAGE_PROFILE``.

    Its parsers run in worker processes (:class:`ParserPool`). When ``AKSHA_SANDBOX`` names
    the bubblewrap backend (``bwrap``, ``bwrap:os-jail`` or ``auto``) and it probes
    available, each parse runs in its own bubblewrap box with read-only access to the one
    input file; otherwise in a plain worker with the same limits.
    """
    try:
        profile = profile_named(config.image_profile)
    except ValueError as e:
        raise StoreError("image_profile_unknown", str(e)) from None
    return UploadIngester(
        profile=profile, parse=parser_pool(config), files=native_file_input(config)
    )


def native_file_input(config: RuntimeConfig) -> FileInput | None:
    """The reading model's native file input and the operator's opt-in (ADR-P07 G6).

    Only in live mode: the generation adapter's declared ``file_types`` (the one
    ``AKSHA_ALIAS_GENERATE`` names, else the provider's built-in), enabled only when
    ``AKSHA_NATIVE_FILES=on``. None when the model declares none, so intake records no
    file route; with the opt-in off every file is routed ``text`` and the reason recorded.
    """
    if config.mode != "live":
        return None
    env = config.live_env
    value = (env.get("AKSHA_NATIVE_FILES") or "off").strip().lower()
    if value not in ("on", "off"):
        raise StoreError("native_files_invalid", "AKSHA_NATIVE_FILES is on or off")
    path = env.get(live_env_name(IMPLICIT_GENERATION_ALIAS)) or live_defaults(env).get("generation")
    try:
        caps = getattr(import_class(path), "capabilities", None) if path else None
    except (ImportError, AttributeError, TypeError, ValueError):
        return None  # the binder names the bad setting when the run starts
    if not isinstance(caps, ModelCapabilities) or not caps.file_types:
        return None
    return FileInput(caps.file_types, caps.max_inline_bytes, enabled=value == "on")


def parser_pool(config: RuntimeConfig) -> ParserPool:
    """The parser workers for uploads, jailed in bubblewrap when it is configured (above)."""
    name = (config.sandbox or "").partition(":")[0]
    if name not in ("bwrap", AUTO):
        return ParserPool()
    box = BwrapSandbox()
    if not box.probe().available:
        return ParserPool()
    scratch = config.sandboxes.root / "parse"
    scratch.mkdir(parents=True, exist_ok=True)
    return ParserPool(jail=box.jail_argv, scratch=scratch)


class ToolQueryRunner:
    """Re-executes a recorded query by calling the read tool that recorded it (ADR-P05 D6).

    A recorded query's ID is ``<agent>.<tool>?<canonical JSON arguments>``; its version is
    the result-set hash the tool returned as a ``query`` read. Re-executing it calls the same
    tool body again with the same arguments over the owner's current inputs, and answers
    the version it returns now. A query no loaded tool can answer, or whose tool now
    fails, answers the empty hash, which differs from any recorded one: the work built on it
    reopens rather than staying current on an answer nobody can check.
    """

    def __init__(
        self,
        loaded: Sequence[LoadedDefinition],
        reader: Callable[[str], Any],
        tables: Mapping[str, AliasTable] | None = None,
    ) -> None:
        """Answer queries of ``loaded``'s tools, reading through ``reader(owner)``.

        ``tables`` holds each agent's bound aliases: a tool that asks fast-class models
        (the decide step of a listing) asks them again through the same binding, so the
        query's answer is the one a run would get now (ADR-P07 G1).
        """
        self.loaded = {d.agent.name: d for d in loaded}
        self.reader = reader
        self.tables = dict(tables or {})

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
        table = self.tables.get(agent)
        spec = decl.spec
        models = (
            ToolModelCalls(table, spec.model_aliases, spec.name, owner)
            if table is not None and spec.model_aliases
            else None
        )
        ctx = ToolContext(owner, agent, "requery", None, f"requery/{query}", 1,
                          reader=self.reader(owner), models=models)  # fmt: skip
        try:
            outcome = decl.body(ctx, args)
        except (TransientStepError, ModelCallFailed):  # a model call failed: a failed query
            return ""
        if not isinstance(outcome.result, Ok):
            return ""
        for read in outcome.result.reads:
            if read.kind == "query" and read.id == query:
                return read.version
        return ""


BUILT_IN_BACKENDS: Mapping[str, type[BwrapSandbox] | type[e2b.E2BSandbox]] = {
    "bwrap": BwrapSandbox,
    "e2b": e2b.E2BSandbox,
}
"""The sandbox backends ``AKSHA_SANDBOX`` can choose (ADR-P12, decision 37)."""
LOCAL_BACKENDS = ("bwrap",)
"""The backends ``auto`` may choose: never a cloud one."""

_TIERS: dict[tuple[object, ...], ExecutionTier] = {}
"""One probed tier per setting per process: probes are side-effect free and cached."""


def _backends(settings: SandboxSettings) -> dict[str, Callable[[], Sandbox]]:
    """A factory per built-in backend, each keeping its session workspaces under ``root``."""
    root = settings.root
    return {
        "bwrap": lambda: BwrapSandbox(workspaces=root / "bwrap", origin=settings.workspace),
        "e2b": lambda: e2b.E2BSandbox(
            state_dir=root / "e2b", template=settings.e2b_template,
            timeout_s=settings.e2b_timeout_s,
            on_end="kill" if settings.e2b_on_end == "kill" else "pause",
            api_url=settings.e2b_api_url, envd_url=settings.e2b_envd_url,
        ),
    }  # fmt: skip


def session_workspaces(config: RuntimeConfig) -> dict[str, SessionWorkspaces]:
    """Every built-in backend's session workspaces, by backend name, probing nothing."""
    built = _backends(config.sandboxes)
    return {name: cast(SessionWorkspaces, built[name]()) for name in BUILT_IN_BACKENDS}


def clean_sandbox_session(config: RuntimeConfig, owner: str, session: str) -> dict[str, Any]:
    """Remove ``session``'s workspaces from every backend that has one (decision 37).

    A bubblewrap folder is deleted (a git worktree is removed, its branch and commits kept);
    an E2B sandbox is killed, which needs ``E2B_API_KEY``. What each backend removed, or
    why it could not, by backend name.
    """
    ref = SessionRef(owner, session)
    out: dict[str, Any] = {}
    for name, workspaces in session_workspaces(config).items():
        try:
            removed = workspaces.clean(ref)
        except (SandboxRefused, OSError, e2b.E2BError) as exc:
            out[name] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        if removed is not None:
            out[name] = dict(removed)
    return out


def _choose_sandbox(
    setting: str | None, settings: SandboxSettings
) -> tuple[Sandbox | None, tuple[Availability, ...], Availability | None]:
    """``AKSHA_SANDBOX`` to (backend, probes, declared outer box); see the table above."""
    if setting is None or setting == "none":
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
    if colon and name in BUILT_IN_BACKENDS and strength != BUILT_IN_BACKENDS[name].strength:
        raise StoreError("sandbox_invalid", f"AKSHA_SANDBOX={setting!r}: {name} is "
                         f"{BUILT_IN_BACKENDS[name].strength}")  # fmt: skip
    try:
        box, probes = select_backend(name, _backends(settings), auto=LOCAL_BACKENDS)
    except SandboxRefused as refused:
        raise StoreError("sandbox_invalid", refused.message) from refused
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
    cache_key = (config.sandbox, config.network, config.approved_environments, config.sandboxes)
    base = _TIERS.get(cache_key)
    if base is None:
        box, probes, declared = _choose_sandbox(config.sandbox, config.sandboxes)
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
    search, web = web_ports(config)
    if config.record is not None:
        sink = JsonlSink(config.record)
        search = None if search is None else RecordingSearch(search, sink)
        web = None if web is None else RecordingFetch(web, sink)
    return replace(base, egress=egress, environments=environments, search=search, web=web)


def web_world(config: RuntimeConfig) -> Path | None:
    """The world folder ``AKSHA_SEARCH=world`` serves, or None for another (or no) web."""
    name, _, arg = (config.search or "").partition(":")
    if name != "world":
        return None
    ref = arg or config.web_world
    if not ref:
        raise StoreError("search_invalid", "AKSHA_SEARCH=world serves a world: set "
                         "AKSHA_WEB_WORLD to a bundled world's name or a world folder")  # fmt: skip
    folder = Path(ref)
    if (folder / "web.json").is_file():
        return folder
    bundled = data.worlds() / ref
    if ref.replace("-", "").isalnum() and (bundled / "web.json").is_file():
        return bundled
    raise StoreError("search_invalid", f"no world with a web at {ref!r}")


def fetched_document(url: str, body: bytes, media_type: str, *, caps: Any = None) -> str:
    """A binary page a live fetch got, as document ingestion serves it (ADR-P31).

    The bytes are ingested as a document attached at the URL (origin ``attachment``, its
    parent the URL, its key the page's record ID), named by the URL's last segment:
    the text is the header and the representation (a PDF's pages, an Office file's text),
    or the header alone when no representation could be made, so the model is told. A file
    over a cap (``caps``, the ingestion defaults when None) is refused ``fetch_refused``,
    nothing kept.
    """
    from urllib.parse import urlsplit

    from aksha.extensions.ingest.ingest import ingest
    from aksha.extensions.ingest.types import Refusal

    name = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "index"
    found = ingest(name, body, "attachment", doc_key=web_record_id(url), parent=url,
                   declared_type=media_type, caps=caps)  # fmt: skip
    if isinstance(found, Refusal):
        raise WebError("fetch_refused", f"{url}: {found.cap} {found.value:,} is over the "
                       f"limit {found.limit:,}; nothing was kept")  # fmt: skip
    return found.record_text


def web_ports(config: RuntimeConfig) -> tuple[WebSearch | None, WebFetch | None]:
    """The search and fetch ports ``AKSHA_SEARCH`` names (the table above)."""
    if not config.search:
        return None, None
    name, _, arg = config.search.partition(":")
    if name == "world":
        root = web_world(config)
        assert root is not None
        world = WorldWeb.of_world(root)
        return world, world
    if name == "replay":
        folder = Path(arg)
        replay = ReplayWeb(read_stream(folder, "search"), read_stream(folder, "fetch"))
        return replay, replay
    if name == "brave":  # imported only here: an offline process loads no HTTP client
        from aksha.adapters.search.brave import BASE_ENV as BRAVE_BASE_ENV
        from aksha.adapters.search.brave import DEFAULT_BASE_URL as BRAVE_BASE_URL
        from aksha.adapters.search.brave import KEY_ENV as BRAVE_KEY_ENV
        from aksha.adapters.search.brave import BraveSearch
        from aksha.adapters.web.http import HttpFetch

        key = config.search_env.get(BRAVE_KEY_ENV, "")
        if not key:
            raise StoreError("search_key_missing", f"AKSHA_SEARCH=brave needs {BRAVE_KEY_ENV}; "
                             "the key is read from the environment and never recorded")  # fmt: skip
        if config.network != "on":
            why = ("AKSHA_SEARCH=brave fetches pages over HTTP: set AKSHA_NETWORK=on (each "
                   "fetch is checked by the aksha.web egress policy)")  # fmt: skip
            raise StoreError("network_off", why)
        base = config.search_env.get(BRAVE_BASE_ENV) or BRAVE_BASE_URL
        return BraveSearch(key, base), HttpFetch(convert=fetched_document)
    raise StoreError("search_invalid", f"AKSHA_SEARCH={config.search!r}: use world, "
                     "world:<name or folder> or brave")  # fmt: skip


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
    world = web_world(config)
    if world is not None:  # an offline model is told the world's hints for its agent
        hints = world_hints(world)
        agents = tuple(with_world_hints(a, hints.get(a.agent.name, {})) for a in agents)
    if config.record is not None:
        agents = tuple(recorded_agent(a, JsonlSink(config.record)) for a in agents)
    given = {**configured_systems(config, loaded), **(systems or {})}
    every_system = {**extension_systems(config, loaded, skip=given), **given}
    reader = source_reader(config, loaded)
    inputs = input_workspace(config, loaded)
    ingester = None if inputs is None else upload_ingester(config)  # refuses before the store opens
    tier = execution_tier(config, loaded)  # so does a web it cannot reach
    store = open_event_store(config.storage, clock, writer=True, catalogue=catalogue)
    ids = LogIdSource(store)
    router = None
    if len(agents) >= 2:
        routable = tuple(a.routable() for a in agents)
        router = Router(routable, PurposeClassifier(), ids)
    sandbox = tier.gate_availability
    recorded = tier.record()
    off = {"reuse": "off"} if config.reuse == "off" else {}
    agents = tuple(replace(a, run_extra={**a.run_extra, "sandbox": recorded, **off})
                   for a in agents)  # fmt: skip
    routing = CorrectionRouting(systems=dict(config.corrections), approver=config.approver,
                                approvers=dict(config.approvers))  # fmt: skip
    return Session(store, clock, agents, router=router, sandbox=lambda: sandbox,
                   corrections=routing,
                   blobs=open_blob_store(config.storage), systems=every_system,
                   reader=reader, ids=ids, tier=tier, inputs=inputs, rebuilds=True,
                   ingester=ingester,
                   run_query=None if inputs is None else ToolQueryRunner(
                       loaded, lambda owner: inputs.reader(owner),
                       {a.agent.name: a.table for a in agents}))  # fmt: skip


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

    The exporter is the ``otel`` extension; it needs the optional extra ``aksha-harness[otel]``
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
# tool reads), the world's offline web behind ``web_search`` and ``fetch_url`` (its search
# proxy, ``adapters.search.world``), and a ledgered mock mailbox standing in for counsel.
# Nothing here reaches a network or needs a key.

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
    tier: ExecutionTier | None = None,
) -> LoopSetup:
    kinds = loaded.kinds
    return LoopSetup(
        store=store,
        owner=DEMO_OWNER,
        agent=agent,
        tree=compile_tree(agent.name, agent.tasks),
        registry=registry,
        table=bind_aliases(
            agent.model_aliases, "fake", {**_fake_adapters(loaded), "generate": model}
        ),
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
        tier=tier or ExecutionTier(),
    )


def open_demo(
    work: Path,
    sources: Path,
    requests: Mapping[str, str],
    *,
    web_world: str,
    mailbox: SystemOfRecord | None = None,
) -> DemoRuntime:
    """Open the demo's store in ``work`` and wire both agents over the ``sources`` folder.

    ``requests`` holds each agent's request text by agent name. ``web_world`` names the bundled
    world whose offline web the agents search (the demo package picks it, so the app names no
    world). ``mailbox`` replaces the counsel mailbox (the demo's kill step wraps it); by
    default it is :func:`counsel_mailbox`.
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
        d.agent.name: d
        for d in load_agents([DEFAULT_AGENTS / n for n in DEMO_AGENT_NAMES], [DEFAULT_EXTENSIONS])
    }
    research, patent = loaded["research"], loaded["patent"]
    folder = FolderSource(INPUTS_SOURCE, sources, "input")
    web_root = data.worlds() / web_world
    web = WorldWeb.of_world(web_root)
    tier = ExecutionTier(search=web, web=web)
    hints = world_hints(web_root)
    research_model = world_hinted({"generate": ResearchModel()}, hints.get("research", {}))
    research_setup = _setup(
        store, research, research.agent, research.registry, research_model["generate"],
        requests[research.agent.name], blobs, folder, tier,
    )  # fmt: skip
    world = SourceWorld(DEMO_START.date())
    agent = build_patent_agent(world)
    listed = {t.spec.name: t for t in agent.tools if isinstance(t, ToolDecl)}
    patent_setup = _setup(
        store, patent, agent, ToolRegistry(agent, listed, ()), PatentFake(),
        requests[agent.name], blobs, folder, tier,
    )  # fmt: skip
    send_tools = {"send_packet": ApprovalTool(listed["send_packet"].spec)}
    hashes = {research.agent.name: research.definition_hash, agent.name: patent.definition_hash}
    authority = approval_authority(send_tools, lambda entry: patent.definition_hash)
    box = mailbox or counsel_mailbox(work)
    worker = OutboxWorker(store, DEMO_OWNER, clock, {COUNSEL_MAILBOX: box}, authority=authority)
    tables = {research.agent.name: research_setup.table, agent.name: patent_setup.table}
    queries = ToolQueryRunner(list(loaded.values()), lambda owner: folder, tables)
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
        (self.loaded,) = load_agents(
            [DEFAULT_AGENTS / adapter.agent], [DEFAULT_EXTENSIONS], catalogue=catalogue
        )
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
                  source: EpisodeSource, web: EpisodeWeb | None) -> Session:  # fmt: skip
            return self._session(bound, folder, reader, systems, source, web)

        return self.adapter(build, digest)

    def session(
        self,
        folder: Path,
        reader: RecordReader,
        systems: Mapping[str, object],
        source: EpisodeSource,
        web: EpisodeWeb | None = None,
    ) -> Session:
        """The writer session of one episode's runtime store in ``folder``."""
        return self._session(self.bound, folder, reader, systems, source, web)

    def _session(
        self,
        bound: SessionAgent,
        folder: Path,
        reader: RecordReader,
        systems: Mapping[str, object],
        source: EpisodeSource,
        web: EpisodeWeb | None = None,
    ) -> Session:
        folder.mkdir(parents=True, exist_ok=True)
        clock = FakeClock(EVAL_CLOCK_START)
        store = sqlite.open_store(folder / "aksha.sqlite", clock, writer=True,
                                  catalogue=self.catalogue)  # fmt: skip
        sources = (source,) if source.collections else ()
        tier = ExecutionTier()
        if web is not None:
            proxy = episode_web(web)
            tier = ExecutionTier(search=proxy, web=proxy)
            bound = with_world_hints(bound, web.hints.get(bound.agent.name, {}))
        return Session(store, clock, (bound,), blobs=FilesystemBlobStore(folder / "blobs"),
                       systems=cast(Mapping[str, SystemOfRecord], systems),
                       sources=sources, reader=reader, tier=tier)  # fmt: skip


def episode_web(web: EpisodeWeb) -> WorldWeb:
    """The world's search proxy over an episode's web: fetches and searches recorded."""
    return WorldWeb(load_pages(web.declared), web.read, index=web.index, on_search=web.searched)


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
        lambda: ScriptedResearchAgent(episode_web),
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


# --- Record and replay (ADR-P22 A1) ---------------------------------------------------------
#
# ``aksha run ... --record <folder>`` runs one agent to its end in a store of its own inside
# the folder, with ``AKSHA_RECORD`` set to it: every model call (generation and every
# fast-class call a tool body makes), every search and every fetch is appended as it is
# answered (``adapters.recording``). The person's part is recorded too: the message, the
# uploaded inputs (copied to ``inputs/``) and the answers and approvals given
# (``recording.json``), then the outputs (``outputs.json``) and the run's trace as OTLP
# JSON (``trace/``). ``aksha replay <folder>`` runs the same agent again in a fresh store,
# fully offline: each model call is answered from the recording (``replay`` run mode, a
# mismatch is ``replay_mismatch``), each search and fetch from the recorded answers
# (``ReplayWeb``), on a clock fixed at the recorded start, and the outputs are compared
# with the recorded ones. A model call or a web call that failed is recorded with its
# error and fails again in the replay, so a step retries as it did. Every setting that
# shapes the run comes from the recording, never from the replaying environment: the
# turn factor (``turn_factors``) and the active-time cap, model step timeout, retries and
# backoff (``limits``) each agent ran under. A recording made before ``limits`` was kept
# replays under the agent's own limits, and one made before failed model calls were
# recorded cannot replay a run whose tool step was retried after a model timeout: the
# answers of the attempt that failed are not marked. Record it again.

RECORDING_FORMAT = "aksha-recording/1"
RECORDING_FILE = "recording.json"
OUTPUTS_FILE = "outputs.json"
RECORDING_INPUTS = "inputs"
RECORD_SESSION = "S-record"
MAX_ANSWER_ROUNDS = 50


@dataclass(frozen=True)
class RunScript:
    """The person's part of a recorded run: the agent, the message, inputs and answers.

    ``answers`` are by question ID (a choice by ``choice``); ``approve`` grants every
    approval the run asks for. A question with no answer ends the recording waiting on it.
    """

    agent: str
    text: str
    answers: Mapping[str, str] = field(default_factory=dict)
    approve: bool = False
    owner: str = "local"

    def as_json(self) -> dict[str, Any]:
        """The script as ``recording.json`` keeps it."""
        return {"agent": self.agent, "text": self.text, "answers": dict(self.answers),
                "approve": self.approve, "owner": self.owner}  # fmt: skip


@dataclass(frozen=True)
class ScriptedRun:
    """Where a scripted run ended: its run, its state or outcome, what it still waits on."""

    run: str | None
    outcome: str
    waiting: tuple[str, ...]
    outputs: Mapping[str, Any]
    trace: tuple[Path, ...]


def _outputs(session: Session, owner: str) -> dict[str, Any]:
    """Every latest output of ``owner``, by slot (``research.matrix``, ``kind/key``)."""
    from aksha.runtime.artifacts.versions import all_versions, current
    from aksha.runtime.canonical import thaw

    view = load_view(session.store, owner)
    out: dict[str, Any] = {}
    for kind, key in sorted({(v.ref.kind, v.ref.key) for v in all_versions(view)},
                            key=lambda s: (s[0], s[1] or "")):  # fmt: skip
        found = current(view, kind, key, blobs=session.blobs)
        if found is not None:
            out[kind if key is None else f"{kind}/{key}"] = thaw(found.content)
    return out


def drive_script(session: Session, script: RunScript, files: Mapping[str, bytes]) -> ScriptedRun:
    """Upload ``files``, send the message, answer from the script until the run ends.

    A UTF-8 file goes up as text, any other as its bytes (base64), which ingestion converts.
    """
    import base64

    from aksha.runtime.session import named as n
    from aksha.runtime.session.views import session_status

    owner = script.owner

    def dispatch(command: n.NamedCommand) -> None:
        receipt = session.dispatch(command)
        if not receipt.accepted:
            raise StoreError("script_refused", f"{type(command).__name__}: {receipt.refused}")

    if files:
        text: dict[str, str] = {}
        binary: dict[str, str] = {}
        for rel, body in files.items():
            try:
                text[rel] = body.decode("utf-8")
            except UnicodeDecodeError:
                binary[rel] = base64.b64encode(body).decode("ascii")
        dispatch(n.UploadInputs(owner=owner, files=text, binary=binary,
                                command_id="script-upload"))  # fmt: skip
    dispatch(n.SendMessage(owner=owner, session=RECORD_SESSION, text=script.text,
                           agent=script.agent, command_id="script-message"))  # fmt: skip
    waiting: tuple[str, ...] = ()
    status = None
    for round_no in range(MAX_ANSWER_ROUNDS):
        session.run_until_yield(owner)
        status = session_status(load_view(session.store, owner), RECORD_SESSION)
        if status.run is None or status.run.state != "waiting":
            break
        moved, missing = False, []
        for i, w in enumerate(status.waiting_on):
            cid = f"script-{round_no}-{i}"
            if w.kind == "approval" and w.approval_id and w.signature:
                if script.approve:
                    dispatch(n.Approve(owner=owner, approval_id=w.approval_id,
                                       shown_signature=w.signature, by="person",
                                       command_id=cid))  # fmt: skip
                    moved = True
                else:
                    missing.append(f"approval {w.approval_id}")
                continue
            ids = [q for q, _ in w.questions] or ["choice"]
            given = {q: script.answers[q] for q in ids if q in script.answers}
            if len(given) < len(ids):
                missing += [f"{w.kind} {q}" for q in ids if q not in given]
                continue
            if len(given) == 1:
                command = n.AnswerInterrupt(owner=owner, interrupt_id=w.interrupt_id,
                                            text=next(iter(given.values())), by="person",
                                            command_id=cid)  # fmt: skip
            else:
                command = n.AnswerInterrupt(owner=owner, interrupt_id=w.interrupt_id,
                                            answers=given, by="person",
                                            command_id=cid)  # fmt: skip
            dispatch(command)
            moved = True
        if not moved:
            waiting = tuple(missing)
            break
    run = None if status is None or status.run is None else status.run
    outcome = "waiting" if waiting else (run.outcome or run.state) if run else "not_started"
    return ScriptedRun(run.run if run else None, str(outcome), waiting,
                       _outputs(session, owner), ())  # fmt: skip


def _trace(session: Session, owner: str, run: str | None, out: Path) -> tuple[Path, ...]:
    """The run's trace as OTLP JSON files in ``out``; none without the ``otel`` extra."""
    if run is None:
        return ()
    try:
        return export_trace(session.store, owner, run, out)
    except AkshaError:
        return ()


def _local_store(folder: Path) -> StorageConfig:
    return StorageConfig(sqlite_path=folder / "aksha.sqlite", blob_path=folder / "blobs")


ASKS_ONCE_PER_RUN = "once_per_run"
"""The manifest's ``asks``: a recording made since ADR-P07 G8, when a run began asking
each fast request once. A recording without it asked otherwise, so it no longer replays
and needs recording again."""


def record_run(
    config: RuntimeConfig,
    folder: Path,
    script: RunScript,
    files: Mapping[str, bytes],
    *,
    clock: Clock | None = None,
) -> ScriptedRun:
    """Run ``script`` to its end with every answer recorded into ``folder`` (section doc).

    The folder must be new or empty. The run's store sits in ``folder/store``, so the
    recording holds everything the replay needs and the person's own store is untouched.
    """
    if folder.exists() and any(folder.iterdir()):
        raise StoreError("recording_exists", f"{folder} is not empty: record into a new folder")
    folder.mkdir(parents=True, exist_ok=True)
    for rel, body in files.items():
        target = folder / RECORDING_INPUTS / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    clock = clock or SystemClock()
    started = clock.now()
    cfg = replace(config, storage=_local_store(folder / "store"), record=folder, server_url=None)
    session = open_session(cfg, clock)
    try:
        ended = drive_script(session, script, files)
        trace = _trace(session, script.owner, ended.run, folder / "trace")
        binding = {a.agent.name: a.table.mode for a in session.agents.values()}
        factors = {
            a.agent.name: int(a.run_extra["turn_factor"])
            for a in session.agents.values()
            if "turn_factor" in a.run_extra
        }
        limits = {a.agent.name: run_limits(a.agent) for a in session.agents.values()}
    finally:
        session.store.close()
    manifest = {
        "format": RECORDING_FORMAT,
        "synthetic_notice": "A recording of one run: what the person gave, every model "
        "answer, every search and fetch. Replay it with aksha replay.",
        "started_at": started.isoformat(),
        "mode": config.mode,
        "modes": binding,
        "search": (config.search or "none").partition(":")[0],
        "script": script.as_json(),
        "inputs": sorted(files),
        "run": ended.run,
        "outcome": ended.outcome,
        "waiting": list(ended.waiting),
    }
    if factors:
        manifest["turn_factors"] = factors  # a replay runs under the same limits
    manifest["limits"] = limits  # and the same timeouts, retries and active-time cap
    manifest["asks"] = ASKS_ONCE_PER_RUN  # what the run asks the model (Replayed.older_asks)
    (folder / RECORDING_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                         encoding="utf-8")  # fmt: skip
    (folder / OUTPUTS_FILE).write_text(json.dumps(ended.outputs, indent=2, sort_keys=True)
                                       + "\n", encoding="utf-8")  # fmt: skip
    return replace(ended, trace=trace)


@dataclass(frozen=True)
class Replayed:
    """A replay's end: the run, whether its outputs equal the recorded ones, and the trace."""

    run: ScriptedRun
    same: bool
    differing: tuple[str, ...]
    out: Path
    older: bool = False
    """The recording predates ``limits`` and recorded failed model calls (section doc)."""
    older_asks: bool = False
    """The recording predates asking each fast request once per run (ADR-P07 G8), so the
    replay asks what it never recorded."""


def replay_run(config: RuntimeConfig, folder: Path, out: Path) -> Replayed:
    """Run a recording again fully offline in ``out`` and compare its outputs (section doc)."""
    path = folder / RECORDING_FILE
    if not path.is_file():
        raise StoreError("recording_missing", f"{folder} holds no {RECORDING_FILE}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("format") != RECORDING_FORMAT:
        raise StoreError("recording_invalid", f"{path}: format {manifest.get('format')!r}")
    if out.exists() and any(out.iterdir()):
        raise StoreError("replay_exists", f"{out} is not empty: replay into a new folder")
    script_raw = manifest["script"]
    script = RunScript(str(script_raw["agent"]), str(script_raw["text"]),
                       dict(script_raw.get("answers", {})), bool(script_raw.get("approve")),
                       str(script_raw.get("owner", "local")))  # fmt: skip
    inputs = folder / RECORDING_INPUTS
    files = {
        p.relative_to(inputs).as_posix(): p.read_bytes()
        for p in sorted(inputs.rglob("*")) if p.is_file()
    } if inputs.is_dir() else {}  # fmt: skip
    recorded = Recorded(read_stream(folder, "models"))
    factors = {str(k): int(v) for k, v in (manifest.get("turn_factors") or {}).items()}
    limits = {str(k): dict(v) for k, v in (manifest.get("limits") or {}).items()}
    cfg = replace(config, storage=_local_store(out / "store"), mode="replay", record=None,
                  search=f"replay:{folder}", network="off", server_url=None,
                  live_env={}, search_env={}, turn_factors=factors,
                  run_limits=limits)  # fmt: skip
    loaded = load_definitions(cfg)
    adapters = {d.agent.name: recorded_adapters(d.model_aliases.values(), recorded)
                for d in loaded}  # fmt: skip
    clock = FakeClock(datetime.fromisoformat(str(manifest["started_at"])))
    out.mkdir(parents=True, exist_ok=True)
    session = open_session(cfg, clock, adapters=adapters)
    try:
        ended = drive_script(session, script, files)
        trace = _trace(session, script.owner, ended.run, out / "trace")
    finally:
        session.store.close()
    (out / OUTPUTS_FILE).write_text(json.dumps(ended.outputs, indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")  # fmt: skip
    # The same means the same bytes: outputs.json written again equals the recorded file.
    # A key named below is where they differ; a byte difference with equal values (a float
    # that printed otherwise) is named as the file itself.
    want = json.loads((folder / OUTPUTS_FILE).read_text(encoding="utf-8"))
    differing = tuple(sorted(k for k in set(want) | set(ended.outputs)
                             if want.get(k) != ended.outputs.get(k)))  # fmt: skip
    if not differing and (out / OUTPUTS_FILE).read_bytes() != (folder / OUTPUTS_FILE).read_bytes():
        differing = (OUTPUTS_FILE,)
    return Replayed(replace(ended, trace=trace), not differing, differing, out,
                    older="limits" not in manifest,
                    older_asks=manifest.get("asks") != ASKS_ONCE_PER_RUN)  # fmt: skip
