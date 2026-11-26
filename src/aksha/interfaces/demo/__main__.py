"""``python -m aksha.interfaces.demo [--work DIR] [--json FILE]``: run the demo, print it.

``--work`` is the folder the demo writes (default ``var/demo``, emptied first); ``--json``
also writes the full report as JSON, which the demo's test reads.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .story import DemoFailed, run_demo
from .summary import render


def main(argv: list[str] | None = None) -> int:
    """Run the demo and print its summary; non-zero when a check fails."""
    parser = argparse.ArgumentParser(prog="python -m aksha.interfaces.demo")
    parser.add_argument("--work", type=Path, default=Path("var/demo"))
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)
    work: Path = args.work
    if work.exists():
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
