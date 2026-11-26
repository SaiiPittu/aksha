"""``aksha``: the command line, in server mode or embedded mode (decision 22, ADR-P19).

Commands that change state (``run``, ``answer``, ``approve``, ``reject``, ``stop``,
``resume``) find a running server first (``AKSHA_SERVER_URL`` or the server file) and send
it the command; with no server answering they run embedded: the command opens the store
as the writer for its one invocation (exit code 8 when another process holds the lock),
dispatches the command, drives the owner's runs until they yield, and exits. Read
commands (``status``, ``inspect``, ``verify``, ``trace``) open a read-only handle and work
beside a server.

``aksha run --input PATH`` (repeatable: a folder, or files) first uploads those files to
the owner's inputs (``UploadInputs``), then sends the message; ``aksha upload PATH`` uploads
alone. Uploading a file again with other bytes records what moved, and only the work built
on it reopens: the command drives the rebuild runs that opens and reports them.

``aksha run --headless`` starts a headless run (ADR-P11 D12, option B): no approval is
asked, every deny still holds, and the start is refused ``sandbox_required`` unless a
sandbox backend probes available (``AKSHA_SANDBOX``) or an outer box is declared. The
exit codes are in :mod:`aksha.interfaces.wire`. A first Ctrl-C during an embedded run
asks the run to stop (ADR-P18 D7) and exits 6.

``aksha eval`` (ADR-P22) is offline and touches no session store: its line goes whole to
:func:`aksha.interfaces.cli.evals.main`, which keeps the eval exit codes. ``aksha demo``
goes whole to :func:`aksha.interfaces.demo.__main__.main` the same way (``make demo`` runs
it), ``aksha example`` copies a bundled world's input files into a new folder to try
``--input`` on, and ``aksha doctor`` reports the version, the paths in use and the extras, opening
nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

import aksha
from aksha import data
from aksha.app import (
    RuntimeConfig,
    data_home,
    export_trace,
    mock_system_notes,
    open_blob_store,
    open_reader,
    open_session,
    session_capabilities,
)
from aksha.interfaces import wire
from aksha.interfaces.cli import evals
from aksha.interfaces.cli.client import Client, ServerError, find_server
from aksha.interfaces.cli.pending import PENDING_FILE, PendingIds, invocation_key
from aksha.runtime.definitions.errors import LoadError
from aksha.runtime.errors import AkshaError
from aksha.runtime.interrupts.reducers import approvals, interrupts
from aksha.runtime.models.errors import BindRefused
from aksha.runtime.projections.view import load_view
from aksha.runtime.session import named as n
from aksha.runtime.session.errors import CommandIdReused
from aksha.runtime.session.service import SYSTEM_UNAVAILABLE, Session
from aksha.runtime.session.types import CommandReceipt
from aksha.runtime.session.views import (
    RunStatus,
    SessionStatus,
    Waiting,
    inspect_run,
    run_status,
    run_statuses,
)
from aksha.runtime.storage.errors import LockHeld, StoreError

DEFAULT_USER = "local"


class Context:
    """One invocation: the configuration, the owner, the output and the argv for retries."""

    def __init__(self, config: RuntimeConfig, owner: str, out: TextIO, argv: Sequence[str]):
        """Hold what every command handler needs."""
        self.config = config
        self.owner = owner
        self.out = out
        self.argv = list(argv)
        pending = config.storage.sqlite_path.parent / PENDING_FILE
        self.pending = PendingIds(pending)
        self.key = invocation_key(owner, argv)
        self.home = config.storage.sqlite_path.parent

    def say(self, text: str) -> None:
        """One line of output."""
        print(text, file=self.out)

    def emit(self, data: object) -> None:
        """JSON output."""
        print(json.dumps(data, indent=2, sort_keys=True, default=str), file=self.out)

    def command_id(self) -> str:
        """This invocation's command ID, reused by a retry of the same command line."""
        return self.pending.take(self.key)

    def received(self) -> None:
        """The receipt arrived: the next identical command line is a new invocation."""
        self.pending.clear(self.key)
        self.pending.clear(self.key + "/session")

    def server(self) -> Client | None:
        """The running server's client, or None to run embedded."""
        url = find_server(self.config.server_url, self.config.server_file())
        return None if url is None else Client(url, self.owner)


# --- reporting -----------------------------------------------------------------------------


def _report(ctx: Context, run: RunStatus | None) -> int:
    if run is None:
        ctx.say("no run")
        return wire.EXIT_OK
    line = f"{run.run} {run.agent} {run.state}"
    if run.outcome:
        line += f" {run.outcome}"
    if run.headless:
        line += " (headless)"
    ctx.say(line)
    for w in run.waiting_on:
        if w.kind == "approval":
            ctx.say(f"  waiting for approval {w.approval_id}: aksha approve {w.approval_id} "
                    f"--signature {w.signature}")  # fmt: skip
        else:
            label = f" ({w.label})" if w.label else ""
            ctx.say(f"  waiting for {w.kind}{label} {w.interrupt_id}: "
                    f"aksha answer {w.interrupt_id} TEXT")  # fmt: skip
        _asked(ctx, w)
    return wire.run_exit_code(run)


def _asked(ctx: Context, w: Waiting) -> None:
    """What the interrupt asks, so a person knows what they answer or approve."""
    if w.tool is not None:
        ctx.say(f"    approves: {w.tool}")
    for name, value in w.facts:
        ctx.say(f"    {name}: {value}")
    several = len(w.questions) > 1
    for qid, text in w.questions:
        hint = f" (--answer {qid}=TEXT)" if several else ""
        ctx.say(f"    asks{hint}: {text}")
    if w.options:
        ctx.say(f"    options: {', '.join(w.options)}")


def _refused(ctx: Context, receipt: CommandReceipt) -> int:
    ctx.say(f"refused: {receipt.refused} (command {receipt.command_id}, seq {receipt.seq})")
    if receipt.refused == "sandbox_required":
        ctx.say(
            "  a headless run needs a sandbox: set AKSHA_SANDBOX=bwrap (probed), or "
            "AKSHA_SANDBOX=<name>:<strength> for an outer box this process runs in"
        )
    if receipt.refused == SYSTEM_UNAVAILABLE:
        ctx.say("  no system of record for this send; for development, a mock mailbox: "
                "AKSHA_SYSTEMS=counsel_mail=mailbox:<folder> (it delivers to a file, "
                "never to a person), then approve again")  # fmt: skip
    return wire.EXIT_REFUSED


def _embedded(ctx: Context) -> Session:
    for note in mock_system_notes(ctx.config):
        ctx.say(note)
    return open_session(ctx.config)


def _say_unavailable(ctx: Context, session: Session) -> None:
    """The runs this process could not move (an approved send with no system), and why."""
    for run, why in sorted(session.unavailable(ctx.owner).items()):
        ctx.say(f"{run} cannot move in this process: {why}")


def _report_session(ctx: Context, status: SessionStatus) -> int:
    """A session with no run yet: what it waits on (a route question), or nothing."""
    if status.run is not None:
        return _report(ctx, status.run)
    if not status.waiting_on:
        ctx.say("no run")
        return wire.EXIT_OK
    for w in status.waiting_on:
        options = f" (aksha answer {w.interrupt_id} --agent NAME)" if w.kind == "route" else ""
        ctx.say(f"session {status.session} waiting for {w.kind} {w.interrupt_id}{options}")
        _asked(ctx, w)
    return wire.run_exit_code(None, status.waiting_on)


def _mutate(ctx: Context, build: Callable[[str], n.NamedCommand], target: Target) -> int:
    """Dispatch one command in server or embedded mode and report what it moved.

    ``target`` is the run the command moves, or the session whose latest run it is,
    resolved before the command is sent (an answer clears the item it answers).
    """
    command = build(ctx.command_id())
    run, session_id = target
    client = ctx.server()
    if client is not None:
        receipt = client.dispatch(command)
        ctx.received()
        if not receipt.accepted:
            return _refused(ctx, receipt)
        if run is None and session_id is not None:
            data = client.get(f"/sessions/{session_id}")
            if data["run"] is None:
                return _report_session(ctx, _session_from_json(data))
            run = str(data["run"]["run"])
        return _report(ctx, None if run is None else client.wait_for_yield(run))
    session = _embedded(ctx)
    try:
        session.resume(ctx.owner)
        receipt = session.dispatch(command)
        ctx.received()
        if not receipt.accepted:
            return _refused(ctx, receipt)
        if run is None and session_id is not None:
            started = session.status(ctx.owner, session_id).run
            run = None if started is None else started.run
        try:
            session.run_until_yield(ctx.owner)
        except KeyboardInterrupt:
            if run is None:
                raise
            ctx.say(f"stopping {run}")
            session.dispatch(n.Stop(owner=ctx.owner, run_id=run, by="person"))
            session.run_until_yield(ctx.owner)
            _report(ctx, run_status(load_view(session.store, ctx.owner), run))
            return wire.EXIT_STOPPED
        _say_unavailable(ctx, session)
        if run is None and session_id is not None:
            return _report_session(ctx, session.status(ctx.owner, session_id))
        status = None if run is None else run_status(load_view(session.store, ctx.owner), run)
        return _report(ctx, status)
    finally:
        session.store.close()


Target = tuple[str | None, str | None]
"""The run a command moves, or else the session whose latest run it moves."""


def _session_from_json(data: dict[str, object]) -> SessionStatus:
    rows = data.get("waiting_on") or ()
    waiting = tuple(wire.waiting_from_json(w) for w in rows)  # type: ignore[attr-defined]
    return SessionStatus(str(data["session"]), None, waiting, int(str(data.get("seq", 0))))


def _item_target(ctx: Context, kind: str, ident: str) -> Target:
    """The run (or, for a route question, the session) of an interrupt or approval.

    Read from a read-only handle before the command is sent, so the answer cannot clear
    the item first; works beside a server.
    """
    store = open_reader(ctx.config)
    try:
        view = load_view(store, ctx.owner)
    finally:
        store.close()
    if kind == "approval":
        a = approvals(view).get(ident)
        return (None, None) if a is None else (a.run, None)
    i = interrupts(view).get(ident)
    if i is None:
        return (None, None)
    return (i.run, None) if i.run is not None else (None, i.session)


def _recent(client: Client) -> str | None:
    rows = client.get("/runs")["runs"]
    return None if not rows else str(rows[-1]["run"])


# --- commands ------------------------------------------------------------------------------


SKIPPED_PARTS = frozenset({".git", "__pycache__", ".venv", "node_modules"})
"""Folders an input folder's upload never walks into."""


def collect_inputs(paths: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """The files under ``paths`` as ``{relative path: text}``, and what was skipped and why.

    A folder contributes every file below it, at its path relative to the folder; a file
    contributes itself, under its name. Hidden files, and files that are not UTF-8 text
    (a PDF: extract its text first), are skipped and reported. Raises ``FileNotFoundError``
    for a path that does not exist.
    """
    files: dict[str, str] = {}
    skipped: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            raise FileNotFoundError(raw)
        found = (
            [(p, p.relative_to(path).as_posix()) for p in sorted(path.rglob("*")) if p.is_file()]
            if path.is_dir()
            else [(path, path.name)]
        )
        for file, rel in found:
            parts = rel.split("/")
            if any(x.startswith(".") or x in SKIPPED_PARTS for x in parts):
                continue
            try:
                files[rel] = file.read_bytes().decode("utf-8")
            except UnicodeDecodeError:
                skipped.append(f"{rel}: not UTF-8 text (extract a PDF's text to .txt first)")
    return files, skipped


def _upload(ctx: Context, paths: Sequence[str]) -> int | None:
    """Upload ``paths`` to the owner's inputs; None when it went through, else an exit code."""
    try:
        files, skipped = collect_inputs(paths)
    except FileNotFoundError as e:
        ctx.say(f"no such input: {e}")
        return wire.EXIT_USAGE
    for why in skipped:
        ctx.say(f"skipped {why}")
    if not files:
        ctx.say("no input files to upload")
        return wire.EXIT_USAGE
    command = n.UploadInputs(owner=ctx.owner, files=files, command_id=ctx.command_id())
    client = ctx.server()
    if client is not None:
        receipt = client.dispatch(command)
    else:
        session = _embedded(ctx)
        try:
            receipt = session.dispatch(command)
        finally:
            session.store.close()
    ctx.received()
    if not receipt.accepted:
        return _refused(ctx, receipt)
    ctx.say(f"uploaded {len(files)} input files")
    return None


def cmd_upload(ctx: Context, args: argparse.Namespace) -> int:
    """Upload files to the owner's inputs, then drive the rebuild runs a change opens."""
    before = _run_ids(ctx)
    failed = _upload(ctx, args.paths)
    if failed is not None:
        return failed
    client = ctx.server()
    if client is not None:
        return wire.EXIT_OK  # the server's runtime thread drives the rebuild runs
    session = _embedded(ctx)
    try:
        session.run_until_yield(ctx.owner)
        view = load_view(session.store, ctx.owner)
    finally:
        session.store.close()
    opened = [r for r in run_statuses(view) if r.run not in before]
    if not opened:
        ctx.say("no work reopened: no input changed, or none that work was built on")
    code = wire.EXIT_OK
    for run in opened:
        code = _report(ctx, run)
    return code


def _run_ids(ctx: Context) -> set[str]:
    store = open_reader(ctx.config)
    try:
        return {r.run for r in run_statuses(load_view(store, ctx.owner))}
    except AkshaError:
        return set()
    finally:
        store.close()


def cmd_run(ctx: Context, args: argparse.Namespace) -> int:
    """Send a message; with ``--agent`` it starts that agent's run, else the router picks.

    With ``--input``, the files are uploaded to the owner's inputs first.
    """
    if args.input:
        failed = _upload(ctx, args.input)
        if failed is not None:
            return failed
    session_id = args.session or ctx.pending.take(ctx.key + "/session", wire.new_session_id)
    ctx.say(f"session {session_id}")

    def build(cid: str) -> n.NamedCommand:
        return n.SendMessage(owner=ctx.owner, session=session_id, text=args.text,
                             agent=args.agent, headless=args.headless, command_id=cid)  # fmt: skip

    return _mutate(ctx, build, (None, session_id))


def cmd_answer(ctx: Context, args: argparse.Namespace) -> int:
    """Answer an input, choice or route interrupt.

    ``TEXT`` answers the interrupt's one question (or its choice); the runtime files it
    under the question's own ID. With several questions, each takes ``--answer ID=TEXT``.
    """
    answers: dict[str, str] = {}
    for item in args.answer or ():
        qid, sep, text = item.partition("=")
        if not sep or not qid:
            raise wire.WireInvalid(message=f"--answer {item!r}: use ID=TEXT")
        answers[qid] = text
    text = args.text if args.agent is None else None

    def build(cid: str) -> n.NamedCommand:
        return n.AnswerInterrupt(owner=ctx.owner, interrupt_id=args.interrupt, text=text,
                                 answers=answers, agent=args.agent, command_id=cid)  # fmt: skip

    return _mutate(ctx, build, _item_target(ctx, "interrupt", args.interrupt))


def _full_signature(ctx: Context, approval_id: str, shown: str) -> str:
    """A typed prefix of at least 12 hex digits expands to the stored signature it begins."""
    if len(shown.removeprefix("v1:")) >= 64:
        return shown
    store = open_reader(ctx.config)
    try:
        found = approvals(load_view(store, ctx.owner)).get(approval_id)
    finally:
        store.close()
    digits = shown.removeprefix("v1:")
    if (
        found is not None
        and len(digits) >= 12
        and found.signature.removeprefix("v1:").startswith(digits)
    ):
        return found.signature
    return shown


def cmd_approve(ctx: Context, args: argparse.Namespace) -> int:
    """Grant an approval for the signature the person was shown."""
    signature = _full_signature(ctx, args.approval, args.signature)

    def build(cid: str) -> n.NamedCommand:
        return n.Approve(owner=ctx.owner, approval_id=args.approval, shown_signature=signature,
                         command_id=cid)  # fmt: skip

    return _mutate(ctx, build, _item_target(ctx, "approval", args.approval))


def cmd_reject(ctx: Context, args: argparse.Namespace) -> int:
    """Refuse an approval."""

    def build(cid: str) -> n.NamedCommand:
        return n.Reject(owner=ctx.owner, approval_id=args.approval, note=args.note,
                        command_id=cid)  # fmt: skip

    return _mutate(ctx, build, _item_target(ctx, "approval", args.approval))


def cmd_stop(ctx: Context, args: argparse.Namespace) -> int:
    """Ask a run to stop (ADR-P18); exits 6 once it has stopped."""

    def build(cid: str) -> n.NamedCommand:
        return n.Stop(owner=ctx.owner, run_id=args.run, reason=args.reason, command_id=cid)

    return _mutate(ctx, build, (args.run, None))


def cmd_resume(ctx: Context, args: argparse.Namespace) -> int:
    """Recover and drive the owner's in-flight runs until they yield (ADR-P02 D10)."""
    client = ctx.server()
    if client is not None:
        run = args.run or _recent(client)
        return _report(ctx, None if run is None else client.wait_for_yield(run))
    session = _embedded(ctx)
    try:
        session.resume(ctx.owner)
        session.run_until_yield(ctx.owner)
        _say_unavailable(ctx, session)
        statuses = run_statuses(load_view(session.store, ctx.owner))
    finally:
        session.store.close()
    if args.run is not None:
        found = next((s for s in statuses if s.run == args.run), None)
        if found is None:
            ctx.say(f"no run {args.run}")
            return wire.EXIT_REFUSED
        return _report(ctx, found)
    code = wire.EXIT_OK
    for s in statuses:
        if s.state != "ended":
            code = max(code, _report(ctx, s))
    return code


def cmd_status(ctx: Context, args: argparse.Namespace) -> int:
    """Where runs stand, read-only; with RUN, exits with that run's code."""
    store = open_reader(ctx.config)
    try:
        statuses = run_statuses(load_view(store, ctx.owner))
    finally:
        store.close()
    if args.session:
        statuses = tuple(s for s in statuses if s.session == args.session)
    if args.run:
        found = next((s for s in statuses if s.run == args.run), None)
        if found is None:
            ctx.say(f"no run {args.run}")
            return wire.EXIT_REFUSED
        return _report(ctx, found)
    if args.json:
        ctx.emit([wire.run_to_json(s) for s in statuses])
    else:
        for s in statuses:
            _report(ctx, s)
    return wire.EXIT_OK


def cmd_inspect(ctx: Context, args: argparse.Namespace) -> int:
    """The run inspector (ADR-P21 D10): a read over a read-only store that never boots."""
    store = open_reader(ctx.config)
    try:
        report = inspect_run(load_view(store, ctx.owner), args.run)
    finally:
        store.close()
    if report is None:
        ctx.say(f"no run {args.run}")
        return wire.EXIT_REFUSED
    ctx.emit(report)
    return wire.EXIT_OK


def cmd_show(ctx: Context, args: argparse.Namespace) -> int:
    """What the agents saved: every latest output, or one output's content as JSON.

    ``aksha show`` lists the owner's latest artifact versions (``research.matrix@v2``);
    ``aksha show research.matrix`` or ``aksha show research.profile/c01-...`` prints that
    output's latest content. A version an input change has invalidated, or one waiting on
    such a version, is marked stale (its freshness in the invalidation view, ADR-P05 D4)
    until its rebuild saves a new one. A read over a read-only store, beside a server too.
    """
    from aksha.runtime.artifacts.versions import all_versions, current
    from aksha.runtime.canonical import thaw
    from aksha.runtime.invalidation.types import STALE_STATES
    from aksha.runtime.invalidation.view import invalidation_view

    store = open_reader(ctx.config)
    try:
        view = load_view(store, ctx.owner)
        blobs = open_blob_store(ctx.config.storage)
        iv = invalidation_view(view, blobs=blobs)

        def freshness(ref: object) -> str:
            state = iv.versions.get(str(ref))
            return "current" if state is None else str(state.freshness)

        if args.output is None:
            slots = sorted({(v.ref.kind, v.ref.key) for v in all_versions(view)},
                           key=lambda s: (s[0], s[1] or ""))  # fmt: skip
            for kind, key in slots:
                found = current(view, kind, key)
                if found is not None:
                    state = freshness(found.ref)
                    mark = f" (stale: {state})" if state in STALE_STATES else ""
                    ctx.say(f"{found.ref}{mark}")
            return wire.EXIT_OK
        kind, _, key = args.output.partition("/")
        found = current(view, kind, key or None, blobs=blobs)
        shown = None if found is None else freshness(found.ref)
    finally:
        store.close()
    if found is None:
        ctx.say(f"no output {args.output}")
        return wire.EXIT_REFUSED
    ctx.emit({"ref": str(found.ref), "freshness": shown, "content": thaw(found.content)})
    return wire.EXIT_OK


def cmd_verify(ctx: Context, args: argparse.Namespace) -> int:
    """Check the hash chain from the genesis to the head (ADR-P01 Amendment H)."""
    store = open_reader(ctx.config)
    try:
        broken = store.verify()
        seq, head = store.head()
    finally:
        store.close()
    if broken is None:
        ctx.say(f"chain ok: {seq} events, head {head}")
        return wire.EXIT_OK
    ctx.say(f"chain broken at seq {broken.seq}: {broken.reason}")
    return wire.EXIT_CHAIN_BROKEN


def cmd_trace(ctx: Context, args: argparse.Namespace) -> int:
    """Export a run's trace as OTLP JSON Lines, one file per segment (ADR-P21 D8)."""
    store = open_reader(ctx.config)
    try:
        if run_status(load_view(store, ctx.owner), args.run) is None:
            ctx.say(f"no run {args.run}")
            return wire.EXIT_REFUSED
        out = Path(args.out) if args.out else ctx.config.storage.sqlite_path.parent / "traces"
        paths = export_trace(store, ctx.owner, args.run, out)
    finally:
        store.close()
    for p in paths:
        ctx.say(str(p))
    return wire.EXIT_OK


def cmd_capabilities(ctx: Context, args: argparse.Namespace) -> int:
    """The capability record (ADR-P19 D7)."""
    client = ctx.server()
    if client is not None:
        ctx.emit(client.get("/capabilities"))
        return wire.EXIT_OK
    session = _embedded(ctx)
    try:
        ctx.emit(session_capabilities(session, ctx.config).as_json())
    finally:
        session.store.close()
    return wire.EXIT_OK


def cmd_serve(ctx: Context, args: argparse.Namespace) -> int:
    """Run the server: the runtime thread holds the writer lock; the CLI and web are clients."""
    try:
        from aksha.interfaces.http.serve import serve
    except ImportError as e:
        ctx.say(f"aksha serve needs the server extra: {install_hint('server')} ({e.name})")
        return wire.EXIT_ENVIRONMENT
    return serve(ctx.config, args.host, args.port, say=ctx.say)


EXTRAS = {
    "server": ("fastapi", "uvicorn"),
    "otel": ("opentelemetry.sdk", "opentelemetry.exporter.otlp.json.file"),
    "postgres": ("psycopg",),
}
"""Each optional extra and the modules it installs (``pyproject.toml``)."""


def install_hint(extra: str) -> str:
    """The exact, quoted command that adds ``extra``."""
    return f'pip install "{aksha.DISTRIBUTION}[{extra}]"'


def _importable(module: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:  # a missing parent package
        return False


def cmd_example(ctx: Context, args: argparse.Namespace) -> int:
    """Copy a bundled synthetic world's input files into a new folder, to try ``--input``.

    Only ``sources/`` is copied, never the world's answer key. With no world named, it
    lists the bundled ones. A folder that already holds files is refused, so nothing of
    the person's is overwritten.
    """
    worlds = sorted(p.name for p in data.worlds().iterdir() if (p / "sources").is_dir())
    if args.world is None:
        ctx.say("bundled synthetic worlds (aksha example NAME [FOLDER]):")
        for name in worlds:
            ctx.say(f"  {name}")
        return wire.EXIT_OK
    try:
        sources = data.world_path(args.world) / "sources"
    except KeyError:
        ctx.say(f"no bundled world {args.world!r}; there are {', '.join(worlds)}")
        return wire.EXIT_USAGE
    target = Path(args.folder or f"{args.world}-inputs").expanduser()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        ctx.say(f"{target} is not empty: pick a new folder")
        return wire.EXIT_USAGE
    shutil.copytree(sources, target, dirs_exist_ok=True)
    count = sum(1 for p in target.rglob("*") if p.is_file())
    ctx.say(f"copied {count} synthetic input files of {args.world} to {target}")
    ctx.say(f'next: aksha run --agent research --input {target} "<your request>"')
    return wire.EXIT_OK


def cmd_doctor(ctx: Context, args: argparse.Namespace) -> int:
    """The version, the paths in use and which extras are installed; opens nothing."""
    storage = ctx.config.storage
    ctx.say(f"aksha {aksha.__version__} (Python {sys.version.split()[0]})")
    ctx.say(f"home:     {ctx.home}")
    if storage.backend == "sqlite":
        ctx.say(f"store:    sqlite {storage.sqlite_path}")
    else:
        ctx.say("store:    postgres (AKSHA_POSTGRES_DSN)")
    ctx.say(f"blobs:    {storage.blob_path}")
    ctx.say(f"agents:   {', '.join(str(p) for p in ctx.config.agent_paths)}")
    ctx.say(f"mode:     {ctx.config.mode}")
    for extra, modules in EXTRAS.items():
        if all(_importable(m) for m in modules):
            ctx.say(f"{extra}: installed")
        else:
            ctx.say(f"{extra}: missing; add it with {install_hint(extra)}")
    return wire.EXIT_OK


# --- parser and entry point ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The argument parser; a usage error exits 2."""
    p = argparse.ArgumentParser(prog="aksha", description="The Aksha agent harness.")
    p.add_argument("--version", action="version", version=f"aksha {aksha.__version__}")
    p.add_argument(
        "--user",
        default=os.environ.get("AKSHA_USER", DEFAULT_USER),
        help="the owner every command acts for (default: $AKSHA_USER or local)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser(
        "demo", help="the offline change-and-recovery demo (aksha demo --help)", add_help=False
    )
    s.add_argument("rest", nargs=argparse.REMAINDER)

    s = sub.add_parser("example", help="copy a bundled synthetic world's inputs to a folder")
    s.add_argument("world", nargs="?", help="the world (none: list them)")
    s.add_argument("folder", nargs="?", help="a new folder (default: <world>-inputs)")
    s.set_defaults(handler=cmd_example)

    s = sub.add_parser("doctor", help="version, paths in use and installed extras")
    s.set_defaults(handler=cmd_doctor)

    s = sub.add_parser("serve", help="run the server (needs aksha-harness[server])")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(handler=cmd_serve)

    s = sub.add_parser("run", help="send a message; --agent starts that agent's run")
    s.add_argument("text")
    s.add_argument("--agent")
    s.add_argument("--session")
    s.add_argument(
        "--headless",
        action="store_true",
        help="never ask: asks become allows, denies hold; needs AKSHA_SANDBOX",
    )
    s.add_argument(
        "--input",
        action="append",
        metavar="PATH",
        help="a folder or file to upload to your inputs first (repeat for several)",
    )
    s.set_defaults(handler=cmd_run)

    s = sub.add_parser("upload", help="upload files to your inputs; reopens only changed work")
    s.add_argument("paths", nargs="+", metavar="PATH", help="folders or files")
    s.set_defaults(handler=cmd_upload)

    s = sub.add_parser("answer", help="answer an input, choice or route interrupt")
    s.add_argument("interrupt")
    s.add_argument("text", nargs="?", help="the answer to the interrupt's one question")
    s.add_argument("--answer", action="append", metavar="ID=TEXT",
                   help="the answer to question ID (repeat for each question)")  # fmt: skip
    s.add_argument("--agent", help="for a route question: the agent to route to")
    s.set_defaults(handler=cmd_answer)

    s = sub.add_parser("approve", help="grant an approval for the signature you were shown")
    s.add_argument("approval")
    s.add_argument(
        "--signature", required=True, help="the signature shown, or its first 12 or more hex digits"
    )
    s.set_defaults(handler=cmd_approve)

    s = sub.add_parser("reject", help="refuse an approval")
    s.add_argument("approval")
    s.add_argument("--note")
    s.set_defaults(handler=cmd_reject)

    s = sub.add_parser("stop", help="ask a run to stop")
    s.add_argument("run")
    s.add_argument("--reason", default="Stopped by user")
    s.set_defaults(handler=cmd_stop)

    s = sub.add_parser("resume", help="recover and drive in-flight runs until they yield")
    s.add_argument("run", nargs="?")
    s.set_defaults(handler=cmd_resume)

    s = sub.add_parser("status", help="where runs stand (read-only)")
    s.add_argument("run", nargs="?")
    s.add_argument("--session")
    s.add_argument("--json", action="store_true")
    s.set_defaults(handler=cmd_status)

    s = sub.add_parser("inspect", help="the run inspector (read-only)")
    s.add_argument("run")
    s.set_defaults(handler=cmd_inspect)

    s = sub.add_parser("show", help="what the agents saved: list outputs, or print one (read-only)")
    s.add_argument("output", nargs="?", help="an output, such as research.matrix or KIND/KEY")
    s.set_defaults(handler=cmd_show)

    s = sub.add_parser("verify", help="check the hash chain (read-only)")
    s.set_defaults(handler=cmd_verify)

    s = sub.add_parser(
        "trace", help="export a run's trace as OTLP JSON (needs aksha-harness[otel])"
    )
    s.add_argument("run")
    s.add_argument("--out")
    s.set_defaults(handler=cmd_trace)

    s = sub.add_parser("capabilities", help="the capability record")
    s.set_defaults(handler=cmd_capabilities)

    s = sub.add_parser(
        "eval", help="run evals over the fixture worlds (aksha eval --help)", add_help=False
    )
    s.add_argument("rest", nargs=argparse.REMAINDER)
    return p


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None,
         env: dict[str, str] | None = None) -> int:  # fmt: skip
    """Run one command; returns its exit code (see :mod:`aksha.interfaces.wire`)."""
    stream = out or sys.stdout
    arguments = list(sys.argv[1:] if argv is None else argv)
    first = _command_index(arguments)
    if first < len(arguments) and arguments[first] == "eval":
        return evals.main(arguments[first + 1 :])
    if first < len(arguments) and arguments[first] == "demo":
        from aksha.interfaces.demo.__main__ import main as demo

        return demo(arguments[first + 1 :], os.environ if env is None else env)
    args = build_parser().parse_args(arguments)
    try:
        config = RuntimeConfig.from_env(os.environ if env is None else env)
    except StoreError as e:
        print(f"environment: {e.code}: {e.message}", file=stream)
        return wire.EXIT_ENVIRONMENT
    ctx = Context(config, args.user, stream, arguments)
    ctx.home = data_home(os.environ if env is None else env)
    handler: Callable[[Context, argparse.Namespace], int] = args.handler
    try:
        return handler(ctx, args)
    except LockHeld as e:
        ctx.say(f"locked: {e.message}; a server or another command is the writer")
        return wire.EXIT_ENVIRONMENT
    except (StoreError, LoadError, BindRefused) as e:
        ctx.say(f"environment: {e.code}: {e.message}")
        return wire.EXIT_ENVIRONMENT
    except wire.WireInvalid as e:
        ctx.say(f"invalid: {e.message}")
        return wire.EXIT_USAGE
    except CommandIdReused as e:
        ctx.say(f"refused: {e.code}: {e.message}")
        return wire.EXIT_REFUSED
    except ServerError as e:
        ctx.say(f"server: {e.status} {e.code}: {e.message}")
        return _server_exit(e.status)
    except AkshaError as e:
        ctx.say(f"refused: {e.code}: {e.message}")
        return wire.EXIT_REFUSED


def _command_index(arguments: Sequence[str]) -> int:
    """Where the command word sits, past the global ``--user`` option."""
    i = 0
    while i < len(arguments):
        if arguments[i] == "--user":
            i += 2
        elif arguments[i].startswith("--user="):
            i += 1
        else:
            return i
    return i


def _server_exit(status: int) -> int:
    if status in (404, 409):
        return wire.EXIT_REFUSED
    if status == 422:
        return wire.EXIT_USAGE
    if status in (423, 503):
        return wire.EXIT_ENVIRONMENT
    return wire.EXIT_INTERNAL


def entry() -> None:
    """The console script: ``sys.exit(main())``."""
    sys.exit(main())
