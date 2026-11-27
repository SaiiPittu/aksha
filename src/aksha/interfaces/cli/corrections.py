"""``aksha correct``, ``aksha corrections`` and the correction side of approve and reject.

``aksha correct TARGET`` proposes a correction (ADR-P05 C1): ``TARGET`` is a record ID
(``input/exports/reviews.csv``) or an artifact slot, optionally with a version and a JSON
pointer (``research.needs#/needs/0/phrase``). The new value is ``--text`` (a record's full
new text, or a string), ``--file`` (a record's full new text from a file) or ``--value``
(JSON, for an artifact value); at least one ``--evidence`` says why, and ``--cite`` names
what supports it. The proposal waits on the domain owner; ``aksha approve K-0001`` and
``aksha reject K-0001`` decide it, sent under the owner's role with ``--by``. An approved
correction is applied at once and the work built on the old value is rebuilt, which the
command drives and reports. ``aksha corrections [ID]`` lists the corrections, or shows one
with what it reopened, what replaced each output and the replacement's lineage.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aksha.app import open_reader
from aksha.interfaces import wire
from aksha.runtime.corrections.types import Correction
from aksha.runtime.corrections.view import corrections, outcome
from aksha.runtime.projections.view import LogView, load_view
from aksha.runtime.session import named as n
from aksha.runtime.session.views import run_statuses

if TYPE_CHECKING:
    from aksha.interfaces.cli.main import Context

CORRECTION_PREFIX = "K-"


def is_correction(ident: str) -> bool:
    """Whether ``ident`` names a correction (``K-0001``), not an approval."""
    return ident.startswith(CORRECTION_PREFIX)


def _value(args: argparse.Namespace) -> object:
    given = [x for x in (args.text, args.file, args.value) if x is not None]
    if len(given) != 1:
        raise wire.WireInvalid(message="give the new value once: --text, --file or --value")
    if args.file is not None:
        try:
            return Path(args.file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise wire.WireInvalid(message=f"--file {args.file}: {e}") from None
    if args.value is not None:
        try:
            return json.loads(args.value)
        except json.JSONDecodeError as e:
            raise wire.WireInvalid(message=f"--value is JSON: {e}") from None
    return args.text


def _view(ctx: Context) -> LogView:
    store = open_reader(ctx.config)
    try:
        return load_view(store, ctx.owner)
    finally:
        store.close()


def _proposed_at(view: LogView, seq: int) -> Correction | None:
    """The proposal the command accepted at ``seq`` recorded (its facts follow the receipt)."""
    found = [c for c in corrections(view).values() if c.proposed_seq >= seq]
    return min(found, key=lambda c: c.proposed_seq) if found else None


def cmd_correct(ctx: Context, args: argparse.Namespace) -> int:
    """Propose a correction to a record or an artifact value (module doc)."""
    from aksha.interfaces.cli.main import _embedded, _refused

    value = _value(args)
    command = n.ProposeCorrection(owner=ctx.owner, target=args.target, value=value,
                                  evidence=tuple(args.evidence or ()),
                                  cites=tuple(args.cite or ()), reason=args.reason or "",
                                  by=args.by, command_id=ctx.command_id())  # fmt: skip
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
    found = _proposed_at(_view(ctx), receipt.seq)
    if found is None:  # pragma: no cover - an accepted proposal always records one
        ctx.say("proposed")
        return wire.EXIT_OK
    ctx.say(f"proposed {found.correction}: {found.target} ({found.route}), "
            f"waiting for {found.approver}")  # fmt: skip
    by = "" if found.approver == n.DEFAULT_BY else f" --by {found.approver}"
    ctx.say(f"  approve: aksha approve {found.correction}{by}")
    ctx.say(f"  reject:  aksha reject {found.correction}{by} --note TEXT")
    return wire.EXIT_OK


def decide(ctx: Context, ident: str, *, approve: bool, by: str, note: str | None) -> int:
    """``aksha approve K-…`` or ``aksha reject K-…``: decide, then drive what it reopened."""
    from aksha.interfaces.cli.main import _embedded, _refused, _report, _say_unavailable

    build: Callable[[str], n.NamedCommand] = lambda cid: n.DecideCorrection(  # noqa: E731
        owner=ctx.owner, correction_id=ident, approve=approve, note=note or "", by=by,
        command_id=cid,
    )  # fmt: skip
    before = {r.run for r in run_statuses(_view(ctx))}
    client = ctx.server()
    if client is not None:
        receipt = client.dispatch(build(ctx.command_id()))
        ctx.received()
        return _refused(ctx, receipt) if not receipt.accepted else wire.EXIT_OK
    session = _embedded(ctx)
    try:
        session.resume(ctx.owner)
        receipt = session.dispatch(build(ctx.command_id()))
        ctx.received()
        if not receipt.accepted:
            return _refused(ctx, receipt)
        session.run_until_yield(ctx.owner)
        _say_unavailable(ctx, session)
        view = load_view(session.store, ctx.owner)
    finally:
        session.store.close()
    found = outcome(view, ident)
    assert found is not None
    ctx.say(f"{ident} {found.state}")
    code = wire.EXIT_OK
    for run in (r for r in run_statuses(view) if r.run not in before):
        code = _report(ctx, run)
    _affected_lines(ctx, found)
    return code


def _affected_lines(ctx: Context, found: Correction) -> None:
    if found.state != "applied":
        return
    if not found.affected:
        ctx.say("  no output was built on the old value")
    for a in found.affected:
        if a.state == "replaced":
            ctx.say(f"  {a.ref} replaced by {a.replacement}")
            for step in a.lineage:
                ctx.say(f"    read {step}")
        elif a.state == "waiting":
            ctx.say(f"  {a.ref} has a revision waiting for review: {a.replacement}")
        elif a.state == "current":
            ctx.say(f"  {a.ref} still holds (its rebuilt content was equal)")
        else:
            ctx.say(f"  {a.ref} is stale, not yet rebuilt")


def _json(found: Correction) -> dict[str, Any]:
    return {
        "correction": found.correction,
        "target": found.target.as_json(),
        "state": found.state,
        "route": found.route,
        "by": found.by,
        "approver": found.approver,
        "decided_by": found.decided_by,
        "evidence": [e.as_json() for e in found.evidence],
        "value_hash": found.value_hash,
        "applied": dict(found.applied),
        "affected": [
            {
                "ref": a.ref,
                "state": a.state,
                "replacement": a.replacement,
                "lineage": list(a.lineage),
            }
            for a in found.affected
        ],
    }


def cmd_corrections(ctx: Context, args: argparse.Namespace) -> int:
    """List the owner's corrections, or show one with what it reopened (read-only)."""
    view = _view(ctx)
    if args.correction is None:
        every = corrections(view)
        if not every:
            ctx.say("no corrections")
        for c in every.values():
            ctx.say(f"{c.correction} {c.state} {c.target} ({c.route}) by {c.by}")
        return wire.EXIT_OK
    found = outcome(view, args.correction)
    if found is None:
        ctx.say(f"no correction {args.correction}")
        return wire.EXIT_REFUSED
    if args.json:
        ctx.emit(_json(found))
        return wire.EXIT_OK
    ctx.say(f"{found.correction} {found.state} {found.target} ({found.route}) by {found.by}")
    for e in found.evidence:
        ctx.say(f"  evidence: {e.text}" + (f" ({e.ref})" if e.ref else ""))
    if found.decided_by:
        ctx.say(f"  decided by {found.decided_by}" + (f": {found.note}" if found.note else ""))
    _affected_lines(ctx, found)
    return wire.EXIT_OK


def add_parsers(sub: Any) -> None:
    """The ``correct`` and ``corrections`` subcommands."""
    s = sub.add_parser("correct", help="propose a correction to a record or an output value")
    s.add_argument("target", help="a record ID, or an output such as KIND/KEY#/pointer")
    s.add_argument("--text", help="the new text (a record's full text, or a string value)")
    s.add_argument("--file", help="a file holding a record's full new text")
    s.add_argument("--value", help="the new value as JSON (an output's value at the pointer)")
    s.add_argument("--evidence", action="append", metavar="TEXT",
                   help="why the value is wrong (at least one; repeat for several)")  # fmt: skip
    s.add_argument("--cite", action="append", metavar="REF",
                   help="a record or output that supports it (repeat for several)")  # fmt: skip
    s.add_argument("--reason", help="a short reason shown to the domain owner")
    s.add_argument("--by", default=n.DEFAULT_BY,
                   help="who proposes: person (default) or agent:NAME")  # fmt: skip
    s.set_defaults(handler=cmd_correct)

    s = sub.add_parser("corrections", help="list corrections, or show one (read-only)")
    s.add_argument("correction", nargs="?")
    s.add_argument("--json", action="store_true")
    s.set_defaults(handler=cmd_corrections)
