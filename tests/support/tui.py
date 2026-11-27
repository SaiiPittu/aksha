"""The terminal UI's test support: the recorded streams and an in-memory ChatApi.

The fixtures in ``tests/fixtures/tui/`` are synthetic: trimmed from a fake-mode recording of
the bundled stillroom world (the patent agent's conversation: an input question, a choice,
an approval, complete; and the research agent routed, then stopped).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aksha.interfaces.tui.client import EventStream
from aksha.runtime.session.types import CommandReceipt

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tui"
SESSION = "S-ui-0001"


def recorded(name: str) -> list[dict[str, Any]]:
    """A fixture stream's items (events and the synchronized marker), in order."""
    rows = []
    for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if "_fixture" not in row:
            rows.append(row)
    return rows


def events(name: str) -> list[dict[str, Any]]:
    """Only the events of a fixture stream."""
    return [r for r in recorded(name) if r.get("kind") == "event"]


def until(
    rows: list[dict[str, Any]], kind: str, field_value: str | None = None
) -> list[dict[str, Any]]:
    """The rows up to and including the first event of type ``kind`` (naming ``field_value``)."""
    out = []
    for row in rows:
        out.append(row)
        payload = row.get("payload") or {}
        if row.get("type") == kind and (field_value is None or field_value in payload.values()):
            return out
    raise AssertionError(f"no {kind} {field_value or ''} in the fixture")


def sse_text(rows: list[dict[str, Any]]) -> str:
    """Rows as SSE text, the way the server writes them."""
    out = []
    for row in rows:
        name = "synchronized" if row.get("kind") == "synchronized" else row["type"]
        data = json.dumps(row, sort_keys=True, separators=(",", ":"))
        out.append(f"id: {row['seq']}\nevent: {name}\ndata: {data}\n\n")
    return "".join(out)


@dataclass
class FakeApi:
    """An in-memory ChatApi: a scripted stream and a log of every command sent."""

    script: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    artifact_content: dict[str, Any] = field(default_factory=dict)
    session_rows: list[dict[str, Any]] = field(default_factory=list)
    refuse: dict[str, str] = field(default_factory=dict)
    _seq: int = 10_000
    _more: list[dict[str, Any]] = field(default_factory=list)
    _wake: threading.Event = field(default_factory=threading.Event)

    def receipt(self, name: str, **args: Any) -> CommandReceipt:
        self.calls.append((name, args))
        self._seq += 1
        code = self.refuse.get(name)
        return CommandReceipt(f"c-{self._seq}", self._seq, code is None, code, False)

    def push(self, *rows: dict[str, Any]) -> None:
        """Send more events down the open stream."""
        self._more.extend(rows)
        self._wake.set()

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    # --- ChatApi --------------------------------------------------------------------------

    def send_message(self, session: str, text: str, agent: str | None) -> CommandReceipt:
        return self.receipt("send_message", session=session, text=text, agent=agent)

    def steer(self, run: str, text: str, lane: str) -> CommandReceipt:
        return self.receipt("steer", run=run, text=text, lane=lane)

    def answer(
        self,
        interrupt: str,
        *,
        text: str | None = None,
        answers: Mapping[str, str] | None = None,
        agent: str | None = None,
    ) -> CommandReceipt:
        return self.receipt(
            "answer",
            interrupt=interrupt,
            text=text,
            answers=None if answers is None else dict(answers),
            agent=agent,
        )

    def approve(self, approval: str, signature: str) -> CommandReceipt:
        return self.receipt("approve", approval=approval, signature=signature)

    def reject(self, approval: str, note: str | None) -> CommandReceipt:
        return self.receipt("reject", approval=approval, note=note)

    def stop(self, run: str, reason: str) -> CommandReceipt:
        return self.receipt("stop", run=run, reason=reason)

    def upload(self, files: Mapping[str, bytes]) -> CommandReceipt:
        return self.receipt("upload", files=dict(files))

    def sessions(self) -> list[dict[str, Any]]:
        return list(self.session_rows)

    def artifacts(self, session: str) -> list[dict[str, Any]] | None:
        if not self.artifact_content:
            return None
        latest: dict[str, int] = {}
        for ref in self.artifact_content:
            name, _, v = ref.partition("@v")
            latest[name] = max(latest.get(name, 0), int(v))
        return [
            {
                "kind": n.split("/")[0],
                "key": n.partition("/")[2] or None,
                "version": v,
                "media_type": "application/json",
            }
            for n, v in latest.items()
        ]

    def artifact(self, ref: str) -> dict[str, Any] | None:
        if not self.artifact_content:
            return None
        return {"ref": ref, "content": self.artifact_content.get(ref)}

    def documents(self) -> list[dict[str, Any]] | None:
        return None

    def document(self, doc_key: str, version: int | None) -> str | None:
        return None

    def events(self, session: str, cursor: int | None, stop: threading.Event) -> EventStream:
        def connect(after: int | None) -> Iterator[str]:
            yield from sse_text(
                [r for r in self.script if after is None or r["seq"] > after]
            ).splitlines(True)
            while not stop.is_set():
                if self._wake.wait(0.02):
                    self._wake.clear()
                    rows, self._more[:] = list(self._more), []
                    self.script.extend(rows)
                    yield from sse_text(rows).splitlines(True)

        return EventStream(connect, cursor=cursor, stop=stop, sleep=lambda _: None)
