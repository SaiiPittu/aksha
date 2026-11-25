"""``ProcessGroupUnit``: a real child process group as a killable unit (ADR-P18 D6).

The children are this interpreter running a few lines of Python, so nothing leaves the
machine and nothing is installed.
"""

from __future__ import annotations

import signal
import sys

from aksha.adapters.sandbox.process import ProcessGroupUnit
from aksha.runtime.cancellation.kill import kill_boundary
from aksha.runtime.cancellation.ports import KillableUnit
from aksha.runtime.cancellation.types import KillPolicy

OBEYS = "import time; print('ready', flush=True); time.sleep(60)"
IGNORES = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('ready', flush=True); time.sleep(60)"
)
FAST = KillPolicy(grace_seconds=0.3, reap_seconds=5.0)


def _unit(source: str) -> ProcessGroupUnit:
    unit = ProcessGroupUnit([sys.executable, "-c", source])
    unit.start()
    assert unit.ready_line() == "ready"
    return unit


def test_it_fits_the_port() -> None:
    unit: KillableUnit = ProcessGroupUnit([sys.executable, "-c", "pass"])
    assert not unit.settled()


def test_a_child_that_exits_is_settled() -> None:
    unit = ProcessGroupUnit([sys.executable, "-c", "pass"])
    unit.start()
    assert unit.wait(10.0)
    assert unit.settled() and unit.returncode == 0


def test_a_child_that_obeys_sigterm_settles_within_the_grace() -> None:
    unit = _unit(OBEYS)
    assert kill_boundary(unit, FAST) == "settled"
    assert unit.returncode == -signal.SIGTERM


def test_a_child_that_ignores_sigterm_is_killed_after_the_grace() -> None:
    unit = _unit(IGNORES)
    assert kill_boundary(unit, FAST) == "killed"
    assert unit.settled() and unit.returncode == -signal.SIGKILL


def test_a_settle_before_start_means_the_child_never_runs() -> None:
    unit = ProcessGroupUnit([sys.executable, "-c", OBEYS])
    unit.request_settle()
    unit.start()
    assert unit.settled() and unit.returncode is None
    assert unit.wait(0.0)
