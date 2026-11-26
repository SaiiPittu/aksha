"""``aksha eval``: run a suite, run the grader self-tests, rebuild a report, check keys.

=====================================  ==================================================
``aksha eval list``                    suites, their cases and the registered agents
``aksha eval run <suite>``             self-tests, then every case k times per world;
                                       writes ``var/evals/<suite>-<seed>/``
``aksha eval selftest``                the grader self-tests for every world
``aksha eval report <folder>``         rebuild a run's reports from its results store
``aksha eval key-check``               recompute every key's anchors and counts
``aksha eval lock <world>``            write the world's ``world.lock.json``
=====================================  ==================================================

Paths default to the repository layout: ``fixtures/`` and ``evals/suites/`` under
``--root`` (the current folder by default).

``aksha eval`` keeps its own exit codes, because a suite's result is not a run's: 0 when
every case passed and the self-tests held, 1 when a case failed, 2 when anything was
ungradeable or a self-test failed (tier 0 blocks on either), 64 a usage error, 65 a refused
input (a bad case file, an unknown suite, agent or world). :func:`main` is the whole
command; :mod:`aksha.interfaces.cli.main` hands every ``aksha eval`` line to it.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from aksha import app
from aksha.evals.case import load_suite
from aksha.evals.criteria import REGISTRY
from aksha.evals.errors import EvalError
from aksha.evals.key import key_check, load_key
from aksha.evals.report import render_text
from aksha.evals.results import SuiteResults
from aksha.evals.runner import rebuild_report, run_suite
from aksha.evals.selftest.run import self_test
from aksha.evals.world import load_world, write_lock

USAGE = 64
REFUSED = 65


def build_parser() -> argparse.ArgumentParser:
    """A parser for ``aksha eval ...`` lines."""
    parser = argparse.ArgumentParser(prog="aksha", description="A small, offline agent harness.")
    commands = parser.add_subparsers(dest="command", required=True)
    add_parser(commands)
    return parser


def main(argv: Sequence[str]) -> int:
    """Run one ``aksha eval`` command (``argv`` without the leading ``eval``)."""
    try:
        args = build_parser().parse_args(["eval", *argv])
    except SystemExit as e:
        return USAGE if e.code not in (0, None) else 0
    try:
        code: int = args.handler(args)
    except EvalError as e:
        print(f"aksha: {e.code}: {e.message}", file=sys.stderr)
        return REFUSED
    return code


def add_parser(commands: Any) -> None:
    """Add ``eval`` and its subcommands to ``aksha``'s subparsers."""
    p = commands.add_parser("eval", help="run evals over the fixture worlds")
    p.add_argument("--root", type=Path, default=Path("."), help="repository root")
    sub = p.add_subparsers(dest="eval_command", required=True)

    ls = sub.add_parser("list", help="list suites, cases and registered agents")
    ls.set_defaults(handler=cmd_list)

    run = sub.add_parser("run", help="run a suite")
    run.add_argument("suite")
    run.add_argument("--k", type=int, default=None, help="rollouts per case and world")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--case", action="append", default=[], help="run only this case")
    run.add_argument("--agent", default=None, help="run every case with this agent")
    run.add_argument("--out", type=Path, default=None)
    run.add_argument("--no-selftest", action="store_true")
    run.add_argument("--judge", action="store_true", help="run judge criteria (fake judge)")
    run.set_defaults(handler=cmd_run)

    st = sub.add_parser("selftest", help="grader self-tests for every world")
    st.add_argument("--world", action="append", default=[])
    st.add_argument("--out", type=Path, default=None)
    st.set_defaults(handler=cmd_selftest)

    rp = sub.add_parser("report", help="rebuild a run's reports from its results store")
    rp.add_argument("folder", type=Path)
    rp.add_argument("--suite", required=True)
    rp.set_defaults(handler=cmd_report)

    kc = sub.add_parser("key-check", help="recompute every key's anchors and counts")
    kc.add_argument("--world", action="append", default=[])
    kc.set_defaults(handler=cmd_key_check)

    lk = sub.add_parser("lock", help="write a world's world.lock.json")
    lk.add_argument("world")
    lk.set_defaults(handler=cmd_lock)


def _worlds(root: Path, names: list[str]) -> list[str]:
    if names:
        return names
    base = root / "fixtures" / "worlds"
    return sorted(p.name for p in base.iterdir() if (p / "key.json").is_file())


def _git(root: Path) -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return {"runtime_commit": "unknown", "dirty": True}
    return {"runtime_commit": commit, "dirty": dirty}


def cmd_list(args: argparse.Namespace) -> int:
    """Print suites, cases and agents."""
    suites = args.root / "evals" / "suites"
    for folder in sorted(p for p in suites.iterdir() if (p / "suite.toml").is_file()):
        suite = load_suite(folder)
        print(f"{suite.name}: {suite.description}")
        for c in suite.cases:
            print(
                f"  {c.id:32} {c.kind}/{c.origin:13} {','.join(c.agents):10} {','.join(c.worlds)}"
            )
    print("agents: " + ", ".join(app.eval_agents().names()))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run a suite and print its text report."""
    suite = load_suite(args.root / "evals" / "suites" / args.suite)
    out = args.out or args.root / "var" / "evals" / f"{suite.name}-{args.seed}"
    run = run_suite(
        suite,
        fixtures_root=args.root / "fixtures",
        agents=app.eval_agents(),
        open_store=app.eval_store_opener(),
        out=out,
        seed=args.seed,
        k=args.k,
        judge=app.eval_judge() if args.judge else None,
        selftests=not args.no_selftest,
        cases=args.case,
        agent_override=args.agent,
        conditions=_git(args.root),
    )
    print(run.reports["report.txt"].read_text(encoding="utf-8"), end="")
    print(f"written: {out}")
    return run.exit_code


def cmd_selftest(args: argparse.Namespace) -> int:
    """Run the grader self-tests and print one line per bundle."""
    out = args.out or args.root / "var" / "evals" / "_selftest"
    ok = True
    for name in _worlds(args.root, args.world):
        world = load_world(name, args.root / "fixtures")
        report = self_test(
            world,
            load_key(world),
            open_store=app.eval_store_opener(),
            out=out,
            registry=REGISTRY,
        )
        killed, total = report.coverage
        print(
            f"{name}: self-tests {'pass' if report.ok else 'FAIL'}; "
            f"mutation coverage {killed} of {total}"
        )
        for check in report.checks:
            print(f"  {'ok' if check.ok else 'FAIL':4} {check.bundle}")
            for problem in check.problems if not check.ok else ():
                print(f"       {problem}")
        for gap in report.key_gaps:
            print(f"  gap  {gap}: ungradeable until the key gains its field")
        ok = ok and report.ok
    return 0 if ok else 2


def cmd_report(args: argparse.Namespace) -> int:
    """Rebuild the reports of an earlier run and print the text one."""
    results, code = rebuild_report(app.eval_store_opener(), args.folder, args.suite)
    _print(results)
    return code


def _print(results: SuiteResults) -> None:
    print(render_text(results), end="")


def cmd_key_check(args: argparse.Namespace) -> int:
    """Recompute every key's anchors and derivable counts against its sources."""
    bad = 0
    for name in _worlds(args.root, args.world):
        world = load_world(name, args.root / "fixtures")
        drifts = key_check(world, load_key(world))
        print(f"{name}: {len(drifts)} drifts")
        for d in drifts:
            print(f"  {d}")
        bad += len(drifts)
    return 1 if bad else 0


def cmd_lock(args: argparse.Namespace) -> int:
    """Write ``world.lock.json`` for one world."""
    world = load_world(args.world, args.root / "fixtures")
    path = write_lock(world.root)
    print(f"written: {path}")
    return 0
