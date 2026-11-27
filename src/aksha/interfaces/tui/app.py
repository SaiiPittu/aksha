"""The chat app: one transcript column, a composer that cards replace, a drawer, pickers.

Everything goes through a :class:`~aksha.interfaces.tui.ports.ChatApi` (HTTP in use, a
double in tests). The session's stream runs in a thread worker and posts batches of
events; the app folds them with the pure :class:`~aksha.interfaces.tui.reducer.Transcript`
and redraws only the items that changed.

What waits on a person replaces the composer inline: a question card (numbered options and
free text) or an approval card that shows the exact tool, facts, targets and the full
signature. ``1`` approves exactly that signature, ``2`` rejects with a note, and Esc only
dismisses the card back to the composer; nothing ever approves on its own. The
conversation continues in the same session: answer, steer, follow up, ask again.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click, Key
from textual.widgets import Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from aksha.interfaces import wire
from aksha.interfaces.tui.client import ApiError, Resynced, StreamItem, Synced
from aksha.interfaces.tui.ports import ChatApi
from aksha.interfaces.tui.reducer import AgentTurn, Approval, Item, Question, Transcript
from aksha.interfaces.tui.render import (
    ACCENT,
    AMBER,
    GREEN,
    MUTED,
    RED,
    diff_renderable,
    item_renderable,
    status_text,
    view_kind,
    view_renderable,
)
from aksha.interfaces.tui.screens import NEW_SESSION, ChatCommands, HelpScreen, SessionPicker
from aksha.runtime.session.types import CommandReceipt

LANES = ("steer", "follow_up", "amend")
LANE_LABEL = {"steer": "steer", "follow_up": "follow-up", "amend": "amend"}
QUIT_WINDOW_S = 3.0
"""A second Ctrl+C within this many seconds quits."""
TITLE_CHARS = 60
DRAIN_S = 0.05
"""How often the app folds what the stream thread received."""
UPLOAD_LIMIT_FILES = 500


class ItemView(Static):
    """One transcript item; a click expands a long agent turn."""

    def __init__(self, item: Item) -> None:
        """Draw ``item``."""
        super().__init__(classes=f"item item-{item.kind}")
        self.item = item
        self.expanded = False
        self.redraw()

    def redraw(self) -> None:
        """Draw the item as it is now."""
        self.update(item_renderable(self.item, expanded=self.expanded))
        if isinstance(self.item, Question | Approval):
            self.set_class(self.item.state == "open", "waiting")

    def on_click(self, _: Click) -> None:
        """Expand or fold an agent turn's calls."""
        if isinstance(self.item, AgentTurn):
            self.expanded = not self.expanded
            self.redraw()


class Composer(Input):
    """The message box; Tab cycles the lane while a run is open, ``?`` alone shows help."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("tab", "app.cycle_lane", "Lane", show=True, priority=False),
    ]

    async def _on_key(self, event: Key) -> None:
        """``?`` in an empty composer opens the help; ``y`` or ``n`` answer a stop question."""
        app: ChatApp = self.app  # type: ignore[assignment]
        if app.confirming is not None and event.character in ("y", "n"):
            event.prevent_default()
            event.stop()
            app.action_confirm_stop() if event.character == "y" else app.action_cancel_stop()
            return
        if event.character == "?" and not self.value:
            event.prevent_default()
            event.stop()
            app.action_help()
            return
        await super()._on_key(event)


class QuestionCard(Vertical):
    """A question in place of the composer: numbered options and free text."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_card", "Dismiss to composer"),
    ]

    question: Question | None = None

    def compose(self) -> ComposeResult:
        """The header, the questions, one answer box per question."""
        yield Static(id="q-head")
        yield Static(id="q-body")
        yield Vertical(id="q-inputs")
        yield Static(id="q-foot")

    def show(self, q: Question) -> None:
        """Show ``q`` (keeps what is typed if it is already shown)."""
        if self.question is not None and self.question.id == q.id:
            return
        self.question = q
        head = Text.assemble((f"◆ Question · {q.qkind} · {q.id}", f"bold {AMBER}"))
        head.append(f"  {q.label}", style=AMBER)
        self.query_one("#q-head", Static).update(head)
        body = Text()
        for _, asked in q.questions:
            body.append(asked + "\n", style="bold")
        for n, option in enumerate(q.options, start=1):
            body.append(f"  {n} ", style=f"bold reverse {AMBER}")
            body.append(f" {option}\n")
        self.query_one("#q-body", Static).update(body)
        self.set_foot(
            Text("Enter sends · Esc back to the composer (the run keeps waiting)", style=MUTED)
        )
        self.call_later(self.swap_inputs, q)

    async def swap_inputs(self, q: Question) -> None:
        """One answer box per question, in place of the last question's."""
        box = self.query_one("#q-inputs", Vertical)
        await box.remove_children()
        hint = "type a number or an answer" if q.options else "type the answer"
        inputs = [
            Input(placeholder=f"{qid}: {hint}, Enter sends", classes="q-input")
            for qid, _ in q.questions or [("answer", "")]
        ]
        await box.mount_all(inputs)
        if self.question is q and self.has_class("shown"):
            inputs[0].focus()

    def set_foot(self, text: Text) -> None:
        """The card's status line."""
        self.query_one("#q-foot", Static).update(text)

    def action_dismiss_card(self) -> None:
        """Back to the composer; the question stays open."""
        app: ChatApp = self.app  # type: ignore[assignment]
        app.dismiss_card()

    @on(Input.Submitted, ".q-input")
    def submitted(self, event: Input.Submitted) -> None:
        """The last box sends every answer; an earlier one moves to the next."""
        event.stop()
        q = self.question
        if q is None:
            return
        inputs = list(self.query(".q-input").results(Input))
        index = inputs.index(event.input)
        if index + 1 < len(inputs):
            inputs[index + 1].focus()
            return
        values = [i.value.strip() for i in inputs]
        values = [pick(q, v) for v in values]
        if not all(values):
            self.set_foot(Text("every question needs an answer", style=RED))
            return
        app: ChatApp = self.app  # type: ignore[assignment]
        app.send_answer(q, values)


def pick(q: Question, value: str) -> str:
    """A typed number picks that option; anything else is the answer as typed."""
    if q.options and value.isdigit() and 1 <= int(value) <= len(q.options):
        return q.options[int(value) - 1]
    return value


class ApprovalCard(Vertical):
    """An approval in place of the composer: exactly what would run, and its signature."""

    can_focus = True
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("1", "approve", "Approve"),
        Binding("2", "reject", "Reject…"),
        Binding("escape", "dismiss_card", "Dismiss to composer"),
    ]

    approval: Approval | None = None

    def compose(self) -> ComposeResult:
        """The details, the choices and the note box for a rejection."""
        yield Static(id="a-body")
        yield Input(placeholder="why reject? (optional) Enter rejects · Esc cancels", id="a-note")
        yield Static(id="a-foot")

    def show(self, a: Approval) -> None:
        """Show ``a``."""
        if self.approval is not None and self.approval.id == a.id:
            return
        self.approval = a
        note = self.query_one("#a-note", Input)
        note.display = False
        note.value = ""
        self.query_one("#a-body", Static).update(approval_details(a))
        self.set_foot(Text("The run waits here. Nothing is sent until you approve.", style=MUTED))
        self.call_after_refresh(self.take_focus, a)

    def take_focus(self, a: Approval) -> None:
        """Focus the card if it still shows ``a``."""
        if self.approval is a and self.has_class("shown"):
            self.focus()

    def set_foot(self, text: Text) -> None:
        """The card's status line."""
        self.query_one("#a-foot", Static).update(text)

    def action_approve(self) -> None:
        """Approve exactly the signature shown."""
        if self.approval is None or self.approval.state != "open":
            return
        app: ChatApp = self.app  # type: ignore[assignment]
        app.send_approve(self.approval)

    def action_reject(self) -> None:
        """Ask for an optional note, then reject."""
        note = self.query_one("#a-note", Input)
        note.display = True
        note.focus()

    def action_dismiss_card(self) -> None:
        """Back to the composer; the approval stays open and nothing is decided."""
        app: ChatApp = self.app  # type: ignore[assignment]
        app.dismiss_card()

    @on(Input.Submitted, "#a-note")
    def rejected(self, event: Input.Submitted) -> None:
        """Reject with the note typed."""
        event.stop()
        if self.approval is None or self.approval.state != "open":
            return
        app: ChatApp = self.app  # type: ignore[assignment]
        app.send_reject(self.approval, event.value.strip() or None)

    def on_key(self, event: Key) -> None:
        """Esc in the note box closes the note, not the card."""
        note = self.query_one("#a-note", Input)
        if event.key == "escape" and note.has_focus:
            event.stop()
            event.prevent_default()
            note.display = False
            self.focus()


def approval_details(a: Approval) -> Text:
    """The approval card's text: the tool, each fact, the targets and the full signature."""
    text = Text.assemble((f"◆ Approval needed · {a.tool} · {a.id}", f"bold {AMBER}"))
    text.append("   ▲ leaves Aksha\n", style=f"bold {RED}")
    text.append(f"  {'Action':<12}", style=MUTED)
    text.append(f"{a.tool}\n", style="bold")
    for name, value in a.facts:
        text.append(f"  {name.capitalize():<12}", style=MUTED)
        text.append(f"{value}\n", style="bold")
    if a.targets:
        text.append(f"  {'Targets':<12}", style=MUTED)
        text.append("  ".join(f"▤ {t}" for t in a.targets) + "\n", style=ACCENT)
    text.append(f"  {'Signature':<12}", style=MUTED)
    text.append(a.signature, style="bold")
    text.append(
        "\n  " + " " * 12 + "you approve exactly this action, its facts and targets\n", style=MUTED
    )
    text.append("\n  ")
    text.append(" 1 ", style=f"bold reverse {GREEN}")
    text.append(" Approve      ")
    text.append(" 2 ", style="bold reverse")
    text.append(" Reject (with a note)      ")
    text.append(" Esc ", style="bold reverse")
    text.append(" back to the composer, decide later", style=MUTED)
    return text


@dataclass
class Opened:
    """What the drawer shows: an artifact or a document, at one version."""

    kind: str  # "artifact" or "document"
    name: str  # kind[/key] for an artifact, the path for a document
    version: int
    latest: int
    doc_key: str | None = None
    media_type: str | None = None
    diff: bool = False


class Drawer(Vertical):
    """The right drawer: the session's artifacts and documents, and one opened."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("left_square_bracket", "version(-1)", "Older"),
        Binding("right_square_bracket", "version(1)", "Newer"),
        Binding("d", "diff", "Diff with previous"),
        Binding("escape", "app.toggle_drawer", "Close"),
    ]

    def compose(self) -> ComposeResult:
        """The list above, the opened view below."""
        yield Static(Text("ARTIFACTS & DOCUMENTS", style=f"bold {MUTED}"), id="drawer-title")
        yield OptionList(id="drawer-list")
        yield Static(id="drawer-head")
        with VerticalScroll(id="drawer-scroll"):
            yield Static(id="drawer-view")

    def action_version(self, step: int) -> None:
        """Open the previous or next version."""
        app: ChatApp = self.app  # type: ignore[assignment]
        app.step_version(step)

    def action_diff(self) -> None:
        """Diff the opened version with the one before."""
        app: ChatApp = self.app  # type: ignore[assignment]
        app.toggle_diff()


class ChatApp(App[int]):
    """``aksha chat`` (module doc)."""

    TITLE = "aksha"
    COMMANDS = App.COMMANDS | {ChatCommands}
    CSS = """
    Screen { background: #15171c; color: #e6e6e6; }
    #topbar { height: 1; background: #1f232b; padding: 0 1; }
    #main { height: 1fr; }
    #thread { width: 1fr; padding: 0 2 0 2; scrollbar-size-vertical: 1; }
    .item { margin: 1 0 0 0; }
    .item-turn { margin: 1 0 0 0; }
    .item-notice, .item-refusal { margin: 0; }
    .item-user { background: #1d2129; padding: 0 1; border-left: tall #6ea8fe; }
    .waiting { border-left: tall #e3a33b; padding: 0 1; }
    #drawer { width: 52%; min-width: 40; border-left: solid #2c313a; padding: 0 1;
              display: none; background: #181b21; }
    #drawer.open { display: block; }
    #drawer-list { height: auto; max-height: 30%; border: none; background: #181b21; }
    #drawer-head { height: auto; margin: 1 0 0 0; }
    #drawer-scroll { height: 1fr; }
    #dock { height: auto; max-height: 60%; dock: bottom; margin-bottom: 1; }
    #status-line { height: auto; padding: 0 2; }
    #composer-row { height: 3; padding: 0 1; }
    #lane { width: auto; min-width: 12; padding: 1 1 0 1; color: #8a8f98; }
    #composer { width: 1fr; border: tall #2c313a; background: #1b1e25; }
    #composer:focus { border: tall #6ea8fe; }
    QuestionCard, ApprovalCard { height: auto; display: none; margin: 0 1;
                                 border: round #e3a33b; padding: 0 1; background: #1b1d22; }
    QuestionCard.shown, ApprovalCard.shown { display: block; }
    ApprovalCard:focus { border: round #f0b95a; }
    #q-inputs { height: auto; }
    .q-input, #a-note { border: tall #2c313a; background: #15171c; }
    #a-note { display: none; }
    HelpScreen, SessionPicker { align: center middle; }
    #help-box { width: 84; height: auto; border: round #6ea8fe; padding: 1 2;
                background: #1b1e25; }
    #picker-box { width: 96%; height: auto; max-height: 85%; border: round #6ea8fe; padding: 0 1;
                  background: #1b1e25; }
    #picker-list { height: auto; max-height: 30; border: none; background: #1b1e25; }
    #picker-head, #picker-keys { height: 1; margin: 1 0 0 0; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "interrupt", "Stop / quit", priority=True),
        Binding("ctrl+right_square_bracket", "toggle_drawer", "Drawer"),
        Binding("ctrl+s", "sessions", "Sessions"),
        Binding("ctrl+r", "open_waiting", "Waiting card"),
        Binding("f1", "help", "Help"),
        Binding("y", "confirm_stop", "Stop the run", show=False, priority=True),
        Binding("n", "cancel_stop", "Keep running", show=False, priority=True),
    ]

    def __init__(
        self,
        api: ChatApi,
        session: str | None = None,
        *,
        agent: str | None = None,
        prompt: str | None = None,
        files: Mapping[str, bytes] | None = None,
        server_note: str = "",
        clock: Callable[[], float] = time.monotonic,
        cwd: Path | None = None,
    ) -> None:
        """A chat over ``api`` in ``session`` (a new one when None)."""
        super().__init__()
        self.api = api
        self.session = session or wire.new_session_id()
        self.agent = agent
        self.server_note = server_note
        self.clock = clock
        self.cwd = cwd or Path.cwd()
        self.transcript = Transcript(self.session)
        self.views: dict[str, ItemView] = {}
        self.lane = 0
        self.dismissed: set[str] = set()
        self.sending: set[str] = set()
        self.interrupted_at: float | None = None
        self.confirming: str | None = None
        self.stop_flag = threading.Event()
        self.initial = (prompt, dict(files or {}))
        self.opened: Opened | None = None
        self.cache: dict[str, Any] = {}
        self.drawer_rows: list[Opened] = []
        self.inbox: queue.SimpleQueue[tuple[str, StreamItem]] = queue.SimpleQueue()

    # --- layout ---------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Top bar, thread and drawer, the dock (status line, composer, cards), footer."""
        yield Static(id="topbar")
        with Horizontal(id="main"):
            yield VerticalScroll(id="thread")
            yield Drawer(id="drawer")
        with Vertical(id="dock"):
            yield Static(id="status-line")
            with Horizontal(id="composer-row"):
                yield Static(id="lane")
                yield Composer(
                    placeholder="Message the agent · @path attaches · ? help", id="composer"
                )
            yield QuestionCard(id="question")
            yield ApprovalCard(id="approval")
        yield Footer()

    def on_mount(self) -> None:
        """Start the stream; the first prompt goes once the replay has caught up."""
        self.refresh_chrome()
        self.query_one(Composer).focus()
        self.set_interval(DRAIN_S, self.drain)
        self.start_stream()

    def on_unmount(self) -> None:
        """Stop the stream thread."""
        self.stop_flag.set()
        closer = getattr(self.api, "close_streams", None)
        if callable(closer):
            closer()

    # --- the stream -----------------------------------------------------------------------

    def start_stream(self) -> None:
        """Stream the current session from its start."""
        self.stop_flag.set()
        closer = getattr(self.api, "close_streams", None)
        if callable(closer):
            closer()
        self.stop_flag = threading.Event()
        self.stream_session(self.session, self.stop_flag)

    @work(thread=True, exclusive=True, group="stream", exit_on_error=False)
    def stream_session(self, session: str, stop: threading.Event) -> None:
        """Read the stream in a thread into the inbox the app drains."""
        for item in self.api.events(session, None, stop):
            if stop.is_set():
                return
            self.inbox.put((session, item))

    def drain(self) -> None:
        """Fold what the stream thread put in the inbox, as one batch, and redraw."""
        items: list[StreamItem] = []
        while True:
            try:
                session, item = self.inbox.get_nowait()
            except queue.Empty:
                break
            if session == self.session:
                items.append(item)
        if items:
            self.fold_batch(items)

    def fold_batch(self, items: list[StreamItem]) -> None:
        """Fold a batch and redraw what changed."""
        thread = self.query_one("#thread", VerticalScroll)
        follow = thread.scroll_y >= thread.max_scroll_y - 2
        touched: list[str] = []
        synced = False
        for item in items:
            if isinstance(item, Synced):
                self.transcript.synchronized = synced = True
            elif isinstance(item, Resynced):
                self.reset_thread()
            else:
                touched.extend(self.transcript.apply(item))
        self.draw(touched)
        if follow:
            self.call_after_refresh(thread.scroll_end, animate=False)
        self.refresh_chrome()
        if synced:
            self.send_initial()

    def draw(self, touched: list[str]) -> None:
        """Mount new items, redraw changed ones."""
        thread = self.query_one("#thread", VerticalScroll)
        new: list[ItemView] = []
        for item_id in dict.fromkeys(touched):
            item = self.transcript.by_id.get(item_id)
            if item is None:
                continue
            view = self.views.get(item_id)
            if view is None:
                view = ItemView(item)
                self.views[item_id] = view
                new.append(view)
            else:
                view.redraw()
        if new:
            thread.mount_all(new)

    def reset_thread(self) -> None:
        """Forget everything shown (a resync, or another session)."""
        self.transcript = Transcript(self.session)
        self.views.clear()
        self.dismissed.clear()
        self.query_one("#thread", VerticalScroll).remove_children()

    # --- the chrome: top bar, lane, dock --------------------------------------------------

    def refresh_chrome(self) -> None:
        """The top bar, the lane label and which of composer or card shows."""
        t = self.transcript
        bar = Text.assemble((" aksha ", "bold reverse"))
        title = t.title or "New session"
        title = title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1] + "…"
        bar.append(f"  {title}", style="bold")
        last = t.last_run
        if last is not None:
            bar.append(f"  │ {last.agent or ''} agent", style="#6ea8fe")
            bar.append(f"  │ {last.run}", style=MUTED)
        bar.append("  │ ")
        pending = t.pending()
        detail = pending[0].kind if pending else ""
        bar.append_text(status_text(t.status(), detail))
        bar.append(f"  │ {self.session}", style=MUTED)
        if self.server_note:
            bar.append(f"  │ {self.server_note}", style=MUTED)
        if not t.synchronized:
            bar.append("  │ connecting…", style=AMBER)
        self.query_one("#topbar", Static).update(bar)
        lane = self.query_one("#lane", Static)
        if t.open_run is not None:
            lane.update(Text(f"{LANE_LABEL[LANES[self.lane]]} ▸", style=f"bold {AMBER}"))
        else:
            lane.update(Text("message ▸", style=MUTED))
        self.refresh_dock()

    def refresh_dock(self) -> None:
        """A card for the oldest thing waiting on you that was not dismissed, else the composer."""
        waiting = [p for p in self.transcript.pending() if p.id not in self.dismissed]
        question = self.query_one(QuestionCard)
        approval = self.query_one(ApprovalCard)
        row = self.query_one("#composer-row")
        first = waiting[0] if waiting else None
        question.set_class(isinstance(first, Question), "shown")
        approval.set_class(isinstance(first, Approval), "shown")
        row.display = first is None
        if isinstance(first, Question):
            approval.approval = None
            question.show(first)
        elif isinstance(first, Approval):
            question.question = None
            approval.show(first)
        else:
            question.question = None
            approval.approval = None
            composer = self.query_one(Composer)
            if (
                self.focused is None
                or not self.focused.is_attached
                or (self.focused is not composer and not self.query_one(Drawer).has_focus_within)
            ):
                composer.focus()

    def say(self, text: str | Text, tone: str = "info") -> None:
        """One status line above the composer."""
        colour = {"info": MUTED, "ok": GREEN, "warn": AMBER, "bad": RED}.get(tone, MUTED)
        line = text if isinstance(text, Text) else Text(text, style=colour)
        self.query_one("#status-line", Static).update(line)

    def dismiss_card(self) -> None:
        """Put the shown card away; the run keeps waiting (Ctrl+R brings it back)."""
        for p in self.transcript.pending():
            if p.id not in self.dismissed:
                self.dismissed.add(p.id)
                self.say(Text(f"◆ {p.id} still waits on you · Ctrl+R reopens it", style=AMBER))
                break
        self.refresh_dock()
        self.query_one(Composer).focus()

    def action_open_waiting(self) -> None:
        """Bring back the cards dismissed with Esc."""
        if not self.transcript.pending():
            self.say("nothing waits on you")
            return
        self.dismissed.clear()
        self.refresh_dock()

    # --- sending --------------------------------------------------------------------------

    def action_cycle_lane(self) -> None:
        """Steer → follow-up → amend, while a run is open."""
        if self.transcript.open_run is None:
            self.say("no run is open: a message starts a new one")
            return
        self.lane = (self.lane + 1) % len(LANES)
        self.refresh_chrome()
        hints = {
            "steer": "steer: reaches the run's next model call",
            "follow_up": "follow-up: held until the run proposes to finish",
            "amend": "amend: corrects the last answer you gave",
        }
        self.say(hints[LANES[self.lane]])

    @on(Input.Submitted, "#composer")
    def composer_submitted(self, event: Input.Submitted) -> None:
        """Send what is typed: upload any ``@path`` first."""
        text = event.value.strip()
        if not text:
            return
        try:
            message, files = attachments(text, self.cwd)
        except OSError as e:
            self.say(f"cannot attach: {e}", "bad")
            return
        event.input.value = ""
        run = self.transcript.open_run
        lane = LANES[self.lane] if run is not None else None
        self.send(message, files, None if run is None else run.run, lane)

    def send_initial(self) -> None:
        """The prompt and files given on the command line, once."""
        prompt, files = self.initial
        self.initial = (None, {})
        if prompt or files:
            run = self.transcript.open_run
            self.send(
                prompt or "",
                files,
                None if run is None else run.run,
                LANES[self.lane] if run is not None else None,
            )

    @work(thread=True, group="send", exit_on_error=False)
    def send(
        self, text: str, files: Mapping[str, bytes], run: str | None, lane: str | None
    ) -> None:
        """Upload, then message the session or steer its open run."""
        try:
            if files:
                self.call_from_thread(self.say, f"uploading {len(files)} file(s)…")
                receipt = self.api.upload(files)
                if not self.receipt_ok(receipt, "upload"):
                    return
                self.call_from_thread(self.say, f"uploaded {len(files)} file(s)", "ok")
            if not text:
                return
            if run is not None and lane is not None:
                receipt = self.api.steer(run, text, lane)
                what = LANE_LABEL[lane]
            else:
                receipt = self.api.send_message(self.session, text, self.agent)
                what = "message"
            if self.receipt_ok(receipt, what):
                self.call_from_thread(self.say, f"{what} sent", "ok")
        except (ApiError, OSError) as e:
            self.call_from_thread(self.say, f"not sent: {e}", "bad")

    def receipt_ok(self, receipt: CommandReceipt, what: str) -> bool:
        """Say a refusal (from a worker thread); True when accepted."""
        if receipt.accepted:
            return True
        self.call_from_thread(self.say, f"✗ {what} refused: {receipt.refused}", "bad")
        return False

    def send_answer(self, q: Question, values: list[str]) -> None:
        """Answer ``q``: one text, an answer per question, or a route's agent."""
        if q.id in self.sending:
            return
        self.sending.add(q.id)
        self.query_one(QuestionCard).set_foot(Text("sending…", style=MUTED))
        self.answer_worker(q, values)

    @work(thread=True, group="send", exit_on_error=False)
    def answer_worker(self, q: Question, values: list[str]) -> None:
        """Post the answer; a refusal shows on the card."""
        try:
            if q.qkind == "route":
                receipt = self.api.answer(q.id, agent=values[0])
            elif len(q.questions) > 1:
                answers = {qid: v for (qid, _), v in zip(q.questions, values, strict=False)}
                receipt = self.api.answer(q.id, answers=answers)
            else:
                receipt = self.api.answer(q.id, text=values[0])
            self.call_from_thread(self.card_result, q.id, receipt, "answer")
        except (ApiError, OSError) as e:
            self.call_from_thread(self.card_error, q.id, str(e))

    def send_approve(self, a: Approval) -> None:
        """Approve exactly ``a.signature``, the one shown on the card."""
        if a.id in self.sending or a.state != "open":
            return
        self.sending.add(a.id)
        self.query_one(ApprovalCard).set_foot(Text("approving…", style=MUTED))
        self.decide_worker(a, True, None)

    def send_reject(self, a: Approval, note: str | None) -> None:
        """Reject ``a`` with an optional note."""
        if a.id in self.sending or a.state != "open":
            return
        self.sending.add(a.id)
        self.query_one(ApprovalCard).set_foot(Text("rejecting…", style=MUTED))
        self.decide_worker(a, False, note)

    @work(thread=True, group="send", exit_on_error=False)
    def decide_worker(self, a: Approval, approve: bool, note: str | None) -> None:
        """Post the decision."""
        try:
            if approve:
                receipt = self.api.approve(a.id, a.signature)
            else:
                receipt = self.api.reject(a.id, note)
            self.call_from_thread(
                self.card_result, a.id, receipt, "approve" if approve else "reject"
            )
        except (ApiError, OSError) as e:
            self.call_from_thread(self.card_error, a.id, str(e))

    def card_result(self, item_id: str, receipt: CommandReceipt, what: str) -> None:
        """A card's command came back: accepted waits for the stream, refused says why."""
        if receipt.accepted:
            self.say(f"{what} {item_id} accepted", "ok")
            return
        self.card_error(item_id, f"{what} refused: {receipt.refused}")

    def card_error(self, item_id: str, message: str) -> None:
        """Let the card be used again and say what went wrong."""
        self.sending.discard(item_id)
        text = Text(f"✗ {message}", style=RED)
        question = self.query_one(QuestionCard)
        approval = self.query_one(ApprovalCard)
        if question.question is not None and question.question.id == item_id:
            question.set_foot(text)
        elif approval.approval is not None and approval.approval.id == item_id:
            approval.set_foot(text)
        else:
            self.say(text)

    # --- Ctrl+C ---------------------------------------------------------------------------

    def action_interrupt(self) -> None:
        """Once: offer to stop the open run; again within a few seconds: quit."""
        now = self.clock()
        if self.interrupted_at is not None and now - self.interrupted_at <= QUIT_WINDOW_S:
            self.exit(0)
            return
        self.interrupted_at = now
        run = self.transcript.open_run
        if run is None:
            self.say("Ctrl+C again to quit", "warn")
            return
        self.confirming = run.run
        line = Text.assemble((f"■ Stop {run.run}?  ", f"bold {AMBER}"))
        line.append("y", style="bold reverse")
        line.append(" stop it   ")
        line.append("n", style="bold reverse")
        line.append(" keep running   Ctrl+C again quits (the run keeps going)", style=MUTED)
        self.say(line)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """``y`` and ``n`` are keys only while a stop waits for confirmation."""
        if action in ("confirm_stop", "cancel_stop"):
            return self.confirming is not None
        return True

    def action_confirm_stop(self) -> None:
        """Stop the run the confirmation line named."""
        run, self.confirming = self.confirming, None
        self.interrupted_at = None
        if run is not None:
            self.say(f"stopping {run}…", "warn")
            self.stop_worker(run)

    def action_cancel_stop(self) -> None:
        """Keep the run going."""
        self.confirming = None
        self.interrupted_at = None
        self.say("the run keeps going")

    @work(thread=True, group="send", exit_on_error=False)
    def stop_worker(self, run: str) -> None:
        """Post the stop."""
        try:
            receipt = self.api.stop(run, "stopped from aksha chat")
            if self.receipt_ok(receipt, "stop"):
                self.call_from_thread(self.say, f"stop requested for {run}", "warn")
        except (ApiError, OSError) as e:
            self.call_from_thread(self.say, f"stop not sent: {e}", "bad")

    # --- help, palette, sessions ----------------------------------------------------------

    def action_help(self) -> None:
        """The key bindings."""
        self.push_screen(HelpScreen())

    def palette_commands(self) -> list[tuple[str, str, Callable[[], Any]]]:
        """The command palette's entries."""
        return [
            ("Sessions", "resume or start a session (Ctrl+S)", self.action_sessions),
            ("New session", "start a fresh conversation", lambda: self.switch_session(None)),
            ("Artifacts and documents", "open the drawer (Ctrl+])", self.action_toggle_drawer),
            (
                "Open what waits on you",
                "reopen a dismissed card (Ctrl+R)",
                self.action_open_waiting,
            ),
            ("Stop the run", "ask the open run to stop", self.action_interrupt),
            ("Next lane", "steer, follow-up, amend (Tab)", self.action_cycle_lane),
            ("Help", "the key bindings (?)", self.action_help),
            ("Quit", "leave the chat; runs keep going on the server", lambda: self.exit(0)),
        ]

    def action_sessions(self) -> None:
        """The session picker."""
        self.load_sessions()

    @work(thread=True, group="reads", exit_on_error=False)
    def load_sessions(self) -> None:
        """Fetch the sessions, then show the picker."""
        try:
            rows = self.api.sessions()
        except (ApiError, OSError) as e:
            self.call_from_thread(self.say, f"sessions not read: {e}", "bad")
            return
        self.call_from_thread(self.show_picker, rows)

    def show_picker(self, rows: list[dict[str, Any]]) -> None:
        """Push the picker over ``rows``."""
        self.push_screen(SessionPicker(rows, self.session), self.picked)

    def picked(self, choice: str | None) -> None:
        """Resume the chosen session, or start a new one."""
        if choice is None:
            return
        self.switch_session(None if choice == NEW_SESSION else choice)

    def switch_session(self, session: str | None) -> None:
        """Show another session (None: a new one) from its start."""
        self.session = session or wire.new_session_id()
        self.lane = 0
        self.opened = None
        self.cache.clear()
        self.reset_thread()
        self.say(f"session {self.session}")
        self.refresh_chrome()
        self.start_stream()

    # --- the drawer -----------------------------------------------------------------------

    def action_toggle_drawer(self) -> None:
        """Open or close the drawer."""
        drawer = self.query_one(Drawer)
        thread = self.query_one("#thread", VerticalScroll)
        if thread.scroll_y >= thread.max_scroll_y - 2:
            self.call_after_refresh(thread.scroll_end, animate=False)
        drawer.toggle_class("open")
        if drawer.has_class("open"):
            self.load_drawer()
            self.query_one("#drawer-list", OptionList).focus()
        else:
            self.query_one(Composer).focus()

    @work(thread=True, group="reads", exit_on_error=False)
    def load_drawer(self) -> None:
        """The session's artifacts and the owner's documents, from the reads or the stream."""
        rows: list[Opened] = []
        note = ""
        try:
            listed = self.api.artifacts(self.session)
            documents = self.api.documents()
        except (ApiError, OSError) as e:
            listed, documents, note = None, None, f"reads failed: {e}"
        if listed is not None:
            for a in listed:
                name = str(a.get("kind")) + (f"/{a['key']}" if a.get("key") else "")
                latest = int(a.get("version") or 1)
                rows.append(
                    Opened("artifact", name, latest, latest, media_type=a.get("media_type"))
                )
        else:
            for slot in self.transcript.artifacts.values():
                name = slot.kind + (f"/{slot.key}" if slot.key else "")
                rows.append(Opened("artifact", name, slot.versions, slot.versions))
            note = note or "this server has no artifact reads: names from the stream only"
        if documents is not None:
            for d in documents:
                latest = int(d.get("version") or 1)
                rows.append(
                    Opened(
                        "document",
                        str(d.get("path")),
                        latest,
                        latest,
                        doc_key=str(d.get("doc_key")),
                        media_type=d.get("media_type"),
                    )
                )
        else:
            for doc in self.transcript.documents.values():
                rows.append(
                    Opened(
                        "document",
                        doc.path,
                        doc.version,
                        doc.version,
                        doc_key=doc.doc_key,
                        media_type=doc.media_type,
                    )
                )
        self.call_from_thread(self.fill_drawer, rows, note)

    def fill_drawer(self, rows: list[Opened], note: str) -> None:
        """List ``rows`` in the drawer."""
        self.drawer_rows = rows
        options = self.query_one("#drawer-list", OptionList)
        options.clear_options()
        for i, row in enumerate(rows):
            glyph = "▦" if row.kind == "artifact" else "▤"
            text = Text.assemble((f"{glyph} ", ACCENT if row.kind == "artifact" else MUTED))
            text.append(row.name, style="bold" if row.kind == "artifact" else "")
            text.append(f"  v{row.latest}", style=MUTED)
            options.add_option(Option(text, id=str(i)))
        if rows:
            options.highlighted = 0
        else:
            options.add_option(Option(Text("nothing saved yet", style=MUTED), disabled=True))
        if note:
            self.query_one("#drawer-head", Static).update(Text(note, style=AMBER))

    @on(OptionList.OptionSelected, "#drawer-list")
    def drawer_selected(self, event: OptionList.OptionSelected) -> None:
        """Open the chosen artifact or document at its latest version."""
        index = int(event.option.id or 0)
        row = self.drawer_rows[index]
        self.opened = Opened(
            row.kind, row.name, row.latest, row.latest, row.doc_key, row.media_type
        )
        self.show_opened()

    def step_version(self, step: int) -> None:
        """The previous or next version of what is open."""
        if self.opened is None:
            return
        version = min(max(self.opened.version + step, 1), self.opened.latest)
        if version != self.opened.version:
            self.opened.version = version
            self.show_opened()

    def toggle_diff(self) -> None:
        """Diff with the previous version, or back to the plain view."""
        if self.opened is None:
            return
        if self.opened.version <= 1 and not self.opened.diff:
            self.query_one("#drawer-head", Static).update(
                Text("v1 has no previous version to diff with", style=MUTED)
            )
            return
        self.opened.diff = not self.opened.diff
        self.show_opened()

    def show_opened(self) -> None:
        """Draw the header now; fetch the content in a thread."""
        o = self.opened
        if o is None:
            return
        head = Text.assemble((f"{'▦' if o.kind == 'artifact' else '▤'} {o.name}", "bold"))
        for v in range(1, o.latest + 1):
            style = f"bold reverse {ACCENT}" if v == o.version else MUTED
            head.append(" ")
            head.append(f"v{v}", style=style)
        head.append("   [ ] version · d diff · Esc close", style=MUTED)
        self.query_one("#drawer-head", Static).update(head)
        self.query_one("#drawer-view", Static).update(Text("loading…", style=MUTED))
        self.fetch_opened(Opened(**vars(o)))

    @work(thread=True, exclusive=True, group="open", exit_on_error=False)
    def fetch_opened(self, o: Opened) -> None:
        """Fetch the version (and the one before, for a diff) and draw it."""
        try:
            content = self.content_of(o, o.version)
            if content is None:
                what = "GET /artifacts/{ref}" if o.kind == "artifact" else "GET /documents/{key}"
                self.call_from_thread(
                    self.show_view,
                    Text(
                        f"This server has no content reads ({what}).\n{o.name} v{o.version} was "
                        "saved; open it with `aksha show` or the web app.",
                        style=MUTED,
                    ),
                )
                return
            if o.diff and o.version > 1:
                before = self.content_of(o, o.version - 1)
                view: Any = diff_renderable(before, content, f"v{o.version - 1}", f"v{o.version}")
            else:
                view = view_renderable(content, view_kind(content, o.media_type))
            self.call_from_thread(self.show_view, view)
        except (ApiError, OSError) as e:
            self.call_from_thread(self.show_view, Text(f"not read: {e}", style=RED))

    def content_of(self, o: Opened, version: int) -> Any:
        """One version's content, cached by reference."""
        key = f"{o.kind}:{o.name}@v{version}"
        if key not in self.cache:
            if o.kind == "artifact":
                data = self.api.artifact(f"{o.name}@v{version}")
                self.cache[key] = None if data is None else data.get("content")
            else:
                self.cache[key] = self.api.document(o.doc_key or o.name, version)
        return self.cache[key]

    def show_view(self, view: Any) -> None:
        """Put ``view`` in the drawer."""
        self.query_one("#drawer-view", Static).update(view)


def attachments(text: str, cwd: Path) -> tuple[str, dict[str, bytes]]:
    """The message without its ``@path`` words, and the files they name (folders walked).

    A word that starts with ``@`` but names nothing on disk stays in the message.
    """
    words: list[str] = []
    files: dict[str, bytes] = {}
    for word in text.split(" "):
        if word.startswith("@") and len(word) > 1:
            path = (cwd / word[1:]).expanduser()
            if path.is_file():
                files[path.name] = path.read_bytes()
                continue
            if path.is_dir():
                for f in sorted(p for p in path.rglob("*") if p.is_file()):
                    if any(part.startswith(".") for part in f.relative_to(path).parts):
                        continue
                    files[f.relative_to(path).as_posix()] = f.read_bytes()
                    if len(files) > UPLOAD_LIMIT_FILES:
                        raise OSError(f"{word} holds over {UPLOAD_LIMIT_FILES} files")
                continue
        words.append(word)
    return " ".join(words).strip(), files
