"""The chat's modal screens and its command palette: sessions, help, commands."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from aksha.interfaces.tui.render import AMBER, MUTED, STATUS

if TYPE_CHECKING:
    from aksha.interfaces.tui.app import ChatApp

NEW_SESSION = "\x00new"
"""What the session picker returns for "start a new session"."""

KEYS: tuple[tuple[str, str], ...] = (
    ("Enter", "send the message (a run is open: in the lane shown)"),
    ("Tab", "while a run is open: cycle the lane, steer → follow-up → amend"),
    ("@path", "in a message: upload that file or folder first"),
    ("1 / 2", "on an approval: approve exactly what is shown / reject with a note"),
    ("1-9, text", "on a question: pick an option, or type an answer, then Enter"),
    ("Esc", "dismiss a card to the composer (it never approves)"),
    ("Ctrl+R", "reopen the card of what waits on you"),
    ("Ctrl+]", "the drawer: artifacts and documents; Enter opens, [ ] versions, d diff"),
    ("Ctrl+S", "sessions: w waiting on you, n new, Enter resume"),
    ("Ctrl+P", "the command palette"),
    ("Ctrl+C", "once: stop the open run (y confirms); twice: quit"),
    ("? or F1", "this help"),
)


class HelpScreen(ModalScreen[None]):
    """The key bindings."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,question_mark,f1,q", "dismiss", "Close")
    ]

    def compose(self) -> ComposeResult:
        """One table of keys."""
        text = Text.assemble(("Keys\n\n", "bold"))
        for key, what in KEYS:
            text.append(f"  {key:<12}", style=f"bold {AMBER}")
            text.append(f"{what}\n")
        text.append("\nStatus: ", style=MUTED)
        for word in ("waiting", "running", "done", "failed", "idle"):
            glyph, label, colour = STATUS[word]
            text.append(f"{glyph} {label}   ", style=colour)
        text.append("\n\nEsc closes.", style=MUTED)
        with Vertical(id="help-box"):
            yield Static(text)


def session_row(row: Mapping[str, Any]) -> Text:
    """One session: status glyph and word, title, agent, run."""
    status = str(row.get("status") or "idle")
    glyph, word, colour = STATUS.get(status, ("○", status, MUTED))
    waiting = row.get("waiting") or {}
    if status == "waiting" and waiting.get("kind"):
        word = str(waiting.get("kind"))
    if row.get("archived"):
        glyph, word, colour = "○", "archived", MUTED
    text = Text.assemble((f"{glyph} {word:<10}", colour))
    title = str(row.get("title") or row.get("session"))
    title = title if len(title) <= 56 else title[:55] + "…"
    text.append(f" {title:<57}", style="bold" if status == "waiting" else "")
    text.append(f"{row.get('agent') or ''!s:<10}", style=MUTED)
    text.append(f"{row.get('run') or ''!s:<8}", style=MUTED)
    text.append(str(row.get("session")), style=MUTED)
    return text


class SessionPicker(ModalScreen[str | None]):
    """The owner's sessions: resume one, filter to what waits on you, or start anew."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss(None)", "Close"),
        Binding("w", "toggle_waiting", "Waiting on you"),
        Binding("n", "new", "New session"),
    ]

    def __init__(
        self, rows: Iterable[Mapping[str, Any]], current: str, *, waiting_only: bool = False
    ) -> None:
        """Hold the rows to list."""
        super().__init__()
        self.rows = list(rows)
        self.current = current
        self.waiting_only = waiting_only

    def compose(self) -> ComposeResult:
        """A header line and the list."""
        with Vertical(id="picker-box"):
            yield Static(id="picker-head")
            yield OptionList(id="picker-list")
            yield Static(
                Text(
                    "↑↓ move   Enter resume   w waiting on you   n new session   Esc close",
                    style=MUTED,
                ),
                id="picker-keys",
            )

    def on_mount(self) -> None:
        """Fill the list."""
        self.fill()
        self.query_one(OptionList).focus()

    def fill(self) -> None:
        """The rows, filtered when only waiting sessions are wanted."""
        rows = [r for r in self.rows if not self.waiting_only or r.get("status") == "waiting"]
        waiting = sum(1 for r in self.rows if r.get("status") == "waiting")
        head = Text.assemble(("Sessions", "bold"))
        head.append(f"   {len(self.rows)} sessions · ", style=MUTED)
        head.append(f"◆ {waiting} waiting on you", style=AMBER)
        if self.waiting_only:
            head.append("   filter: waiting on you", style=f"bold {AMBER}")
        self.query_one("#picker-head", Static).update(head)
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options([Option(session_row(r), id=str(r.get("session"))) for r in rows])
        if not rows:
            options.add_option(Option(Text("no sessions here", style=MUTED), disabled=True))
        if rows:
            options.highlighted = next(
                (i for i, r in enumerate(rows) if r.get("session") == self.current), 0
            )

    def action_toggle_waiting(self) -> None:
        """Show only sessions waiting on you, or all."""
        self.waiting_only = not self.waiting_only
        self.fill()

    def action_new(self) -> None:
        """Start a new session."""
        self.dismiss(NEW_SESSION)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Resume the chosen session."""
        self.dismiss(event.option.id)


class ChatCommands(Provider):
    """The chat's commands in the command palette (Ctrl+P)."""

    def commands(self) -> list[tuple[str, str, Callable[[], Any]]]:
        """Each command's name, help and callback."""
        app: ChatApp = self.app  # type: ignore[assignment]
        return app.palette_commands()

    async def discover(self) -> Hits:
        """Every command, before anything is typed."""
        for name, help_text, callback in self.commands():
            yield DiscoveryHit(name, callback, help=help_text)

    async def search(self, query: str) -> Hits:
        """The commands that match ``query``."""
        matcher = self.matcher(query)
        for name, help_text, callback in self.commands():
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), callback, help=help_text)
