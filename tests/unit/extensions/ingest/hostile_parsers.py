"""Hostile stand-ins for the parsing function, run inside the parser worker by tests.

Each has the signature of :func:`aksha.extensions.ingest.parse.parse_in_process` and is
named to a :class:`~aksha.extensions.ingest.worker.ParserPool` as ``module:function``. Each
runs the real type check first (so the worker reports it), then misbehaves the way a
hostile file can make a real parser misbehave. Synthetic test code: no real parser bug.
"""

from __future__ import annotations

import os
import signal
import socket
import time
from typing import Any

from aksha.extensions.ingest import sniff
from aksha.extensions.ingest.parse import Parsed, parse_in_process
from aksha.extensions.ingest.types import Caps, Converted, Representation

HERE = __name__


def _typed(name: str, data: bytes, declared: str | None, caps: Caps, sniffed: Any) -> None:
    found = sniff.sniff(data, name, declared, caps=caps)
    if sniffed is not None:
        sniffed(found)


def _said(name: str, data: bytes, declared: str | None, caps: Caps, said: str) -> Parsed:
    return Parsed(sniff.sniff(data, name, declared, caps=caps),
                  Converted(Representation(said)))  # fmt: skip


def sleeps(name: str, data: bytes, declared: str | None, caps: Caps, allow: frozenset[str],
           *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """Types the input, then waits forever without using CPU: only the wall clock stops it."""
    _typed(name, data, declared, caps, sniffed)
    while True:
        time.sleep(60)


def spins(name: str, data: bytes, declared: str | None, caps: Caps, allow: frozenset[str],
          *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """Types the input, then loops on the CPU forever."""
    _typed(name, data, declared, caps, sniffed)
    n = 0
    while True:
        n += 1


def hogs(name: str, data: bytes, declared: str | None, caps: Caps, allow: frozenset[str],
         *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """Types the input, then allocates 64 MiB blocks without bound."""
    _typed(name, data, declared, caps, sniffed)
    held = []
    while True:
        held.append(bytearray(64 * 1024 * 1024))


def segfaults(name: str, data: bytes, declared: str | None, caps: Caps, allow: frozenset[str],
              *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """Types the input, then dies the way a native parser bug does."""
    _typed(name, data, declared, caps, sniffed)
    os.kill(os.getpid(), signal.SIGSEGV)
    raise AssertionError("unreachable")


def lies(name: str, data: bytes, declared: str | None, caps: Caps, allow: frozenset[str],
         *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """Writes a frame header claiming an enormous reply straight to the protocol pipe."""
    os.write(1, (2**62).to_bytes(8, "big") + b"{")
    time.sleep(60)
    raise AssertionError("unreachable")


def pid(name: str, data: bytes, declared: str | None, caps: Caps, allow: frozenset[str],
        *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """Says which process parsed it."""
    return _said(name, data, declared, caps, str(os.getpid()))


def dials(name: str, data: bytes, declared: str | None, caps: Caps, allow: frozenset[str],
          *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """Tries to connect to the TCP port the input names on the host's loopback."""
    port = int(data.decode().strip())
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            said = "connected"
    except OSError as e:
        said = f"blocked: {type(e).__name__}"
    return _said(name, data, declared, caps, said)


def probes(name: str, data: bytes, declared: str | None, caps: Caps, allow: frozenset[str],
           *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """Tries to fork, to write a file and to read a path; reports what it saw."""
    seen: list[str] = []
    try:
        child = os.fork()
        if child == 0:
            os._exit(0)
        os.waitpid(child, 0)
        seen.append("fork: allowed")
    except OSError as e:
        seen.append(f"fork: {type(e).__name__}")
    try:
        with open(f"/tmp/aksha-probe-{os.getpid()}", "wb") as f:
            f.write(b"x")
            f.flush()
        seen.append("write: allowed")
    except OSError as e:
        seen.append(f"write: {type(e).__name__}")
    target = data.decode().strip()
    seen.append("read: allowed" if target and os.path.exists(target) else "read: absent")
    seen.append("env: " + ",".join(sorted(os.environ)))
    return _said(name, data, declared, caps, "\n".join(seen))


def jailed_view(name: str, data: bytes, declared: str | None, caps: Caps,
                allow: frozenset[str], *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """In a box: whether the input is writable and what else of the host it can see."""
    seen: list[str] = []
    try:
        with open("/input/document", "ab"):
            pass
        seen.append("input: writable")
    except OSError as e:
        seen.append(f"input: {type(e).__name__}")
    target = data.decode().strip()
    seen.append("host file: seen" if os.path.exists(target) else "host file: absent")
    seen.append("inputs: " + ",".join(sorted(os.listdir("/input"))))
    return _said(name, data, declared, caps, "\n".join(seen))


def real(name: str, data: bytes, declared: str | None, caps: Caps, allow: frozenset[str],
         *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """The real parsing (a module-level alias tests can name)."""
    return parse_in_process(name, data, declared, caps, allow, sniffed=sniffed)


def malformed(name: str, data: bytes, declared: str | None, caps: Caps, allow: frozenset[str],
              *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """Sends a well-framed reply whose anchor has two fields instead of three."""
    from aksha.extensions.ingest import wire

    reply = {"stage": "parsed",
             "sniffed": {"detected_type": "text/plain", "charset": None, "text": None,
                         "type_conflict": None, "gap": None},
             "converted": {"representation": {"text": "x", "anchors": [["a", 0]]},
                           "gap": None, "children": []},
             "frames": []}  # fmt: skip
    os.write(1, wire.frame(reply))
    time.sleep(60)
    raise AssertionError("unreachable")


def vandal(name: str, data: bytes, declared: str | None, caps: Caps, allow: frozenset[str],
           *, sniffed: Any = None) -> Parsed:  # fmt: skip
    """Tries to truncate, then to delete, the host file the input names; reports each."""
    target = data.decode().strip()
    seen: list[str] = []
    for what in ("truncate", "unlink"):
        try:
            if what == "truncate":
                os.truncate(target, 0)
            else:
                os.unlink(target)
            seen.append(f"{what}: allowed")
        except OSError as e:
            seen.append(f"{what}: {type(e).__name__}")
    return _said(name, data, declared, caps, "\n".join(seen))
