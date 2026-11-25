"""A real ``SIGKILL`` after every commit of a run, then a restart (ADR-P02 D5, ADR-P03 D7).

The child process (``tests/support/killrun.py``) drives a run with a model call, a read
that fails once and is retried, an ``at_most_once`` hook, an effectful step through the
outbox and a handoff drained by the worker, and kills itself right after its ``n``-th
commit, for every ``n`` until the run finishes without a kill. The parent then restarts:
recovery on startup (which runs the outbox phases itself) and the lazy resume. After every
kill point:

1. each step has exactly one result, and the run ends once;
2. the model call was paid once;
3. the hook ran at most once, and only a hook killed after its dispatch marker and before
   its body ran is missing, closed ``interrupted{outcome_unknown}``, never run again;
4. the ledger holds exactly two rows (the effectful step's and the handoff's), and no
   outbox entry is left open;
5. a second recovery finds nothing to do.

SQLite only: the point is a real process death between commits, which the adapter-level
contract tests already cover for PostgreSQL's transactions.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.support.killrun import COMPLETED, recover

ROOT = Path(__file__).resolve().parents[2]
LIMIT = 60
"""More commits than the run makes; reaching it means the loop never finished."""


def _kill_after(d: Path, n: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "tests.support.killrun", str(d), str(n)],
        capture_output=True, text=True, cwd=ROOT, timeout=60, check=False,
    )  # fmt: skip


def test_every_commit_point_recovers_to_one_result_per_step(tmp_path: Path) -> None:
    killed = 0
    for n in range(1, LIMIT):
        d = tmp_path / f"k{n:02d}"
        d.mkdir()
        child = _kill_after(d, n)
        if COMPLETED in child.stdout:
            break
        assert child.returncode == -9, child.stderr[-2000:]
        killed += 1
        seen = recover(d)
        assert len(seen["completed"]) == len(set(seen["completed"])) == 4, (n, seen)
        assert [e["outcome"] for e in seen["ended"]] == ["complete"], (n, seen)
        assert seen["bodies"].count("model") == 1, (n, seen)
        hooks = seen["bodies"].count("hook")
        assert hooks <= 1, (n, seen)
        if hooks == 0:  # killed after the dispatch marker, before the body ran
            assert seen["hook"] is not None
            assert (seen["hook"]["outcome"], seen["hook"]["code"]) == (
                "interrupted", "outcome_unknown",
            ), (n, seen)  # fmt: skip
        assert seen["ledger_rows"] == 2, (n, seen)
        assert seen["outbox_open"] == [], (n, seen)
        assert all(
            not (o.closed_unknown or o.ended or o.in_flight or o.reconciled) for o in seen["again"]
        ), (n, seen)
    else:
        raise AssertionError(f"the run made {LIMIT} or more commits")
    assert killed >= 15  # every commit of the run was a kill point
