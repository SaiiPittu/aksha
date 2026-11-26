"""``aksha demo [--work DIR] [--json FILE]``: run the demo, print it.

``python -m aksha.interfaces.demo`` is the same command. ``--work`` is the folder the demo
writes (default ``<AKSHA_HOME>/demo``, :func:`aksha.app.data_home`). An existing folder is
emptied only when an earlier demo wrote it (it holds the demo's marker file) or it is
empty; any other folder is refused, so a mistyped path never loses data. ``--json``
also writes the full report as JSON, which the demo's test reads.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from aksha.app import data_home

from .story import MARKER, DemoFailed, run_demo
from .summary import render


def default_work(env: Mapping[str, str]) -> Path:
    """The folder the demo writes when ``--work`` is not given."""
    return data_home(env) / "demo"


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Run the demo and print its summary; non-zero when a check fails."""
    parser = argparse.ArgumentParser(
        prog="aksha demo",
        description="The change-and-recovery demo: offline fake models, no keys, about 20 s.",
    )
    parser.add_argument("--work", type=Path, default=None,
                        help="the folder the demo writes (default: $AKSHA_HOME/demo)")  # fmt: skip
    parser.add_argument("--json", type=Path, default=None, help="also write the report as JSON")
    args = parser.parse_args(argv)
    work: Path = args.work or default_work(os.environ if env is None else env)
    if work.exists():
        if not (work / MARKER).is_file() and any(work.iterdir()):
            print(f"{work} exists and was not written by the demo: pick another --work",
                  file=sys.stderr)  # fmt: skip
            return 2
        shutil.rmtree(work)
    print("Running the change-and-recovery demo (about 20 seconds)...", flush=True)
    try:
        report = run_demo(work)
    except DemoFailed as failed:
        print(f"demo failed: {failed}", file=sys.stderr)
        return 1
    print(render(report, str(work)))
    if args.json is not None:
        args.json.write_text(json.dumps(report.as_json(), indent=1), encoding="utf-8")
    ok = report.chain["ok"] and report.kill["killed"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
