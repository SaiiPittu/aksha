"""One episode's tools: the only way an agent under test touches its world.

An :class:`Episode` owns a private copy of the world's ``sources/`` and a fresh event store.
Each tool call is recorded as ``episode.tool_called``; each output, relation, flag, approval
and send as its own ``episode.*`` event. Nothing else the agent does counts: the grader reads
the bundle exported from this store (eval design section 1: graded on what the agent leaves
in the store, never on what it says it did).

The path guard (:func:`guard_path`) refuses an absolute path, a ``..`` segment, a path that
resolves outside the copy (a symlink, for example) and any path naming a file kept out of
reach (``key.json``, ``spec.yaml``, ``manifest.json``, ``world.lock.json``). The world copy
holds only ``sources/``, so the key is not even present to be reached.

Sends go to a fixture receiver whose ledger lives beside the store, not in it. The receiver
accepts a second send under the same effect key, because catching that is the invariant
``inv.one_row_per_key``'s job, not the receiver's.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from aksha.evals.errors import EvalError, PathRefused, ToolFault
from aksha.evals.faults import FaultProfile, decide
from aksha.evals.world import OUT_OF_REACH
from aksha.runtime.canonical import canonical_json, thaw
from aksha.runtime.events.envelope import Event, NewEvent
from aksha.runtime.storage.ports import EventStore

Citation = Mapping[str, Any]


def guard_path(root: Path, rel: str) -> Path:
    """The file ``rel`` names under ``root``, or :class:`PathRefused`."""
    if not rel or rel.startswith(("/", "\\")) or "\\" in rel:
        raise PathRefused(message=f"{rel!r}: a tool path is relative, with forward slashes")
    parts = PurePosixPath(rel).parts
    if any(p in ("..", "") for p in parts) or ":" in parts[0]:
        raise PathRefused(message=f"{rel!r}: no '..' and no drive in a tool path")
    if any(p in OUT_OF_REACH for p in parts):
        raise PathRefused(message=f"{rel!r}: out of every tool's reach")
    base = root.resolve()
    target = (root / rel).resolve()
    if not target.is_relative_to(base):
        raise PathRefused(message=f"{rel!r}: resolves outside the tool root")
    return target


def text_hash(text: str) -> str:
    """``sha256:`` over a text's UTF-8 bytes: how a read is named in the bundle."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class FixtureReceiver:
    """Where sends land: a ledger of every delivery, outside the episode's store."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    def deliver(self, row: Mapping[str, Any]) -> None:
        """Record one delivery; a repeated effect key is recorded again, never refused."""
        self.rows.append(dict(row))


class Episode:
    """The tools of one episode. See the module docstring for what each records."""

    def __init__(
        self,
        *,
        store: EventStore,
        owner: str,
        root: Path,
        case_id: str,
        world: str,
        rollout: int,
        seed: int,
        opening: str = "",
        faults: FaultProfile | None = None,
        approve: Callable[[str, int, str], bool] | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        """Open the episode over ``store`` with the world copy at ``root / "sources"``."""
        self.store = store
        self.owner = owner
        self.root = root
        self.case_id = case_id
        self.world = world
        self.rollout = rollout
        self.seed = seed
        self.opening = opening
        self.faults = faults
        self.receiver = FixtureReceiver()
        self.reads: dict[str, str] = {}
        self.steps = 0
        self.restarts = 0
        self._approve = approve or (lambda _ref, _version, _action: True)
        self._on_event = on_event
        self._attempts: dict[str, int] = {}
        self._versions: dict[str, tuple[int, str]] = {}
        self._approvals = 0
        self._ended = False
        for event in store.read(owner):
            self._replay(event)

    @property
    def sources(self) -> Path:
        """The episode's own copy of ``sources/``."""
        return self.root / "sources"

    def _replay(self, event: Event) -> None:
        """Rebuild counters from an existing store, so a restarted agent sees them."""
        p = thaw(event.payload)
        if event.type == "episode.output_saved":
            base = f"{p['kind']}/{p['key']}"
            self._versions[base] = (int(p["version"]), _digest(p["content"], p["citations"]))
        elif event.type == "episode.approval_requested":
            self._approvals += 1
        elif event.type == "episode.tool_called":
            self.steps += 1

    def _append(
        self, boundary: str, type_: str, payload: Mapping[str, Any], *, notify: bool = True
    ) -> Event:
        if self._ended:
            raise EvalError("episode_ended", f"{type_} after the episode ended")
        (event,) = self.store.append(self.owner, boundary, [NewEvent(type_, payload)])
        if notify:
            self._notify(event)
        return event

    def _notify(self, event: Event) -> None:
        """Tell the environment (change steps may apply here, and may restart the agent)."""
        if self._on_event is not None:
            self._on_event(event)

    def _call(
        self, tool: str, args: Mapping[str, Any], path: str | None = None
    ) -> Callable[[bool, str | None, str | None], None]:
        """Count one step, apply any fault switch, and return the recorder for its result."""
        self.steps += 1
        attempt_key = f"{tool}:{canonical_json(dict(args))}"
        attempt = self._attempts.get(attempt_key, 0) + 1
        self._attempts[attempt_key] = attempt

        def record(ok: bool, result_hash: str | None, error: str | None) -> None:
            self._append(
                "U2",
                "episode.tool_called",
                {
                    "tool": tool,
                    "args": dict(args),
                    "ok": ok,
                    "result_hash": result_hash,
                    "error": error,
                    "attempt": attempt,
                },
            )

        if self.faults is not None:
            step_id = f"{self.case_id}/{tool}/{canonical_json(dict(args))}"
            for rule in self.faults.rules:
                if rule.matches(tool, path) and decide(rule, self.seed, step_id, attempt):
                    record(False, None, rule.code)
                    raise ToolFault(rule.code, f"{tool} {args}: fault switch {rule.code}")
        return record

    # --- reading the world ------------------------------------------------------------

    def list_sources(self, folder: str = "") -> list[str]:
        """Every file under ``folder`` of the world copy, as tool paths, sorted."""
        record = self._call("list_sources", {"folder": folder}, folder or None)
        try:
            base = guard_path(self.root, f"sources/{folder}" if folder else "sources")
        except PathRefused as e:
            record(False, None, e.code)
            raise
        files = sorted(
            p.relative_to(self.sources.resolve()).as_posix() for p in base.rglob("*") if p.is_file()
        )
        record(True, text_hash("\n".join(files)), None)
        return files

    def read_source(self, path: str) -> str:
        """The raw text of one source file in the world copy."""
        record = self._call("read_source", {"path": path}, path)
        try:
            target = guard_path(self.sources, path)
            text = target.read_text(encoding="utf-8")
        except PathRefused as e:
            record(False, None, e.code)
            raise
        except OSError:
            record(False, None, "not_found")
            raise EvalError("not_found", f"no source file {path!r}") from None
        h = text_hash(text)
        self.reads[h] = text
        record(True, h, None)
        return text

    # --- what the agent leaves behind ---------------------------------------------------

    def save(
        self,
        kind: str,
        key: str,
        content: Mapping[str, Any],
        citations: Sequence[Citation] = (),
    ) -> str:
        """Save one output version; returns its ref ``<kind>/<key>@v<n>``.

        Saving content and citations equal to the latest version records nothing and
        returns the latest ref, so a resumed agent does not mint versions for unchanged work.
        """
        base = f"{kind}/{key}"
        digest = _digest(content, citations)
        latest = self._versions.get(base)
        if latest is not None and latest[1] == digest:
            return f"{base}@v{latest[0]}"
        version = (latest[0] + 1) if latest else 1
        event = self._append(
            "U2",
            "episode.output_saved",
            {
                "ref": base,
                "kind": kind,
                "key": key,
                "version": version,
                "content": dict(content),
                "citations": [dict(c) for c in citations],
            },
            notify=False,
        )
        self._versions[base] = (version, digest)
        self._notify(event)
        return f"{base}@v{version}"

    def version(self, base: str) -> int | None:
        """The latest saved version of ``<kind>/<key>``, or None."""
        latest = self._versions.get(base)
        return latest[0] if latest else None

    def relate(
        self,
        type_: str,
        src: str,
        dst: str,
        excerpt: Citation | None = None,
        label: str | None = None,
    ) -> None:
        """Save a typed relation (``same_entity_as``, ``contradicts``, ``family_member_of``)."""
        self._append(
            "U2",
            "episode.relation_saved",
            {
                "type": type_,
                "src": src,
                "dst": dst,
                "excerpt": dict(excerpt) if excerpt else None,
                "label": label,
            },
        )

    def flag(self, kind: str, text: str, file: str | None = None) -> None:
        """Record a flag, such as ``instruction_like`` text seen in a source."""
        self._append("U2", "episode.flagged", {"kind": kind, "text": text, "file": file})

    def request_approval(self, target_ref: str, action: str) -> str | None:
        """Ask the scripted user to approve ``action`` on ``target_ref`` (``<kind>/<key>``).

        Returns the approval ID when granted, None when refused. The approval is bound to
        the target's current version, so a later version needs a fresh approval.
        """
        version = self.version(target_ref)
        if version is None:
            raise EvalError("target_missing", f"no saved output {target_ref}")
        self._approvals += 1
        approval_id = f"A-{self._approvals:04d}"
        self._append(
            "U2",
            "episode.approval_requested",
            {
                "approval_id": approval_id,
                "target_ref": target_ref,
                "target_version": version,
                "action": action,
            },
        )
        granted = bool(self._approve(target_ref, version, action))
        self._append(
            "U4",
            "episode.approval_decided",
            {"approval_id": approval_id, "granted": granted, "by": "scripted_user"},
        )
        return approval_id if granted else None

    def send(
        self,
        recipient: str,
        action: str,
        *,
        target_ref: str | None = None,
        approval_id: str | None = None,
        effect_key: str | None = None,
    ) -> str:
        """Send an effect to the fixture receiver; returns its effect key.

        The default effect key is the action, recipient and target version, so the same
        send twice carries one key.
        """
        version = self.version(target_ref) if target_ref else None
        key = effect_key or f"{action}:{recipient}:{target_ref or '-'}@v{version or 0}"
        args = {"recipient": recipient, "action": action, "target_ref": target_ref}
        record = self._call("send", args, None)
        row = {
            "effect_key": key,
            "recipient": recipient,
            "action": action,
            "target_ref": target_ref,
            "target_version": version,
            "approval_id": approval_id,
        }
        self.receiver.deliver(row)
        record(True, None, None)
        self._append("U2", "episode.effect_sent", row)
        return key

    def end(self, outcome: str = "finished", reason: str = "") -> None:
        """Record the episode's end; the environment calls it, never the agent."""
        self._append(
            "U3",
            "episode.ended",
            {"outcome": outcome, "reason": reason, "restarts": self.restarts},
        )
        self._ended = True


def _digest(content: Mapping[str, Any], citations: Sequence[Citation]) -> str:
    body = canonical_json({"content": thaw(content), "citations": [thaw(c) for c in citations]})
    return hashlib.sha256(body.encode()).hexdigest()
