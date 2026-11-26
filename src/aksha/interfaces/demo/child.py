"""The send that dies: ``python -m aksha.interfaces.demo.child <work> <sources> <run>``.

It opens the demo's store, drives the rebuild run that waits to send the new packet, and
wraps the counsel mailbox so that right after the mailbox records a delivery the process
sends itself ``SIGKILL``. Nothing in Python runs after that: the outbox entry stays
dispatched with no receipt, and the run stays open. The parent then recovers.
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksha.app import counsel_mailbox, open_demo
from aksha.runtime.loop.drive import drive
from aksha.runtime.outbox.ports import SystemOfRecord
from aksha.runtime.outbox.types import SystemReceipt

from .story import REQUESTS


class DiesAfterDelivery:
    """A mailbox that delivers, then kills the process before anything records it."""

    def __init__(self, inner: SystemOfRecord) -> None:
        """Wrap ``inner``."""
        self.inner = inner

    def ledger_id(self) -> str:
        """The wrapped ledger's ID."""
        return self.inner.ledger_id()

    def apply(self, key: str, request: Mapping[str, Any]) -> SystemReceipt:
        """Deliver through the wrapped mailbox, then die."""
        self.inner.apply(key, request)  # the row is on disk (fsync) before this returns
        os.kill(os.getpid(), signal.SIGKILL)
        raise AssertionError("unreachable: the process is dead")

    def lookup(self, key: str, request: Mapping[str, Any]) -> SystemReceipt | None:
        """Look up through the wrapped mailbox."""
        return self.inner.lookup(key, request)


def main(work: Path, sources: Path, run: str) -> None:
    """Drive ``run`` until the mailbox delivery kills the process."""
    rt = open_demo(work, sources, REQUESTS, mailbox=DiesAfterDelivery(counsel_mailbox(work)))
    drive(rt.patent, rt.executor(), run)
    print("the run finished without reaching the mailbox", file=sys.stderr)
    sys.exit(3)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
