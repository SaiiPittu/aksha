"""A bench for the session API tests: the scribe fixture agent behind one ``Session``.

The scribe (``tests/fixtures/agents/scribe``) writes one note; a request that says "ask"
asks a question first, one that says "publish" asks to publish the note (an external
effect that needs approval). The bench passes an in-memory ``Ledger`` as ``board.ledger``,
in place of the board extension's file ledger, so a test can read its rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aksha.adapters.clock.fake import FakeClock
from aksha.app import RuntimeConfig, StorageConfig, open_session
from aksha.runtime.events.envelope import Event
from aksha.runtime.session.service import Session
from tests.support.events import START
from tests.support.systems import Ledger

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
AGENTS = FIXTURES / "agents"
EXTENSIONS = FIXTURES / "extensions"
OWNER = "o1"
SESSION = "S-0001"


def scribe_only(tmp: Path) -> Path:
    """A folder of agent folders holding only the scribe (a symlink to the fixture)."""
    root = tmp / "agents"
    root.mkdir(parents=True, exist_ok=True)
    link = root / "scribe"
    if not link.exists():
        link.symlink_to(AGENTS / "scribe", target_is_directory=True)
    return root


def config(tmp: Path, **changes: Any) -> RuntimeConfig:
    """A configuration over a SQLite file in ``tmp`` with only the scribe loaded."""
    storage = StorageConfig(sqlite_path=tmp / "var" / "aksha.sqlite", blob_path=tmp / "var" / "b")
    values: dict[str, Any] = {
        "storage": storage,
        "agent_paths": (scribe_only(tmp),),
        "extension_paths": (EXTENSIONS,),
    }
    values.update(changes)
    return RuntimeConfig(**values)


def env(tmp: Path, **extra: str) -> dict[str, str]:
    """The environment variables of :func:`config`, for a CLI subprocess."""
    out = {
        "AKSHA_SQLITE_PATH": str(tmp / "var" / "aksha.sqlite"),
        "AKSHA_BLOB_PATH": str(tmp / "var" / "b"),
        "AKSHA_AGENTS": str(scribe_only(tmp)),
        "AKSHA_EXTENSIONS": str(EXTENSIONS),
    }
    out.update(extra)
    return out


class SessionBench:
    """One writer session over the scribe, with the board ledger as its system of record."""

    def __init__(self, tmp: Path, **changes: Any) -> None:
        self.tmp = tmp
        self.clock = FakeClock(START)
        self.board = Ledger("board")
        self.config = config(tmp, **changes)
        self.session: Session = open_session(self.config, self.clock,
                                             systems={"board.ledger": self.board})  # fmt: skip

    def events(self, type_: str | None = None) -> list[Event]:
        return [e for e in self.session.store.read(OWNER) if type_ is None or e.type == type_]

    def payloads(self, type_: str) -> list[Mapping[str, Any]]:
        return [e.payload for e in self.events(type_)]

    def close(self) -> None:
        self.session.store.close()
