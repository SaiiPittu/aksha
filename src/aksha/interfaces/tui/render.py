"""Transcript items, statuses and opened views as Rich text, for the Textual widgets.

One status vocabulary, shared with the web app, and never colour alone: each status has a
glyph and a word as well as a colour (``STATUS``).
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from aksha.interfaces.tui.reducer import (
    AgentTurn,
    Approval,
    Item,
    Line,
    Notice,
    Outcome,
    Question,
    Refusal,
    UserMessage,
)

AMBER = "#e3a33b"
BLUE = "#6ea8fe"
GREEN = "#5fbf7f"
RED = "#ef6b62"
MUTED = "#8a8f98"
ACCENT = "#a99cf5"

STATUS: Mapping[str, tuple[str, str, str]] = {
    "waiting": ("◆", "waiting on you", AMBER),
    "running": ("●", "running", BLUE),
    "done": ("✓", "done", GREEN),
    "complete": ("✓", "complete", GREEN),
    "failed": ("✗", "failed", RED),
    "blocked": ("✗", "blocked", RED),
    "partial": ("◐", "partial", AMBER),
    "stopped": ("■", "stopped", MUTED),
    "idle": ("○", "idle", MUTED),
}
"""Each status word's glyph, label and colour."""

TONES: Mapping[str, str] = {"info": MUTED, "ok": GREEN, "warn": AMBER, "bad": RED, "run": BLUE}


def status_text(status: str, detail: str = "") -> Text:
    """``◆ waiting on you · approval``: glyph, word and an optional detail."""
    glyph, word, colour = STATUS.get(status, ("○", status, MUTED))
    text = Text.assemble((f"{glyph} {word}", f"bold {colour}"))
    if detail:
        text.append(f" · {detail}", style=colour)
    return text


def duration(ms: int | None) -> str:
    """A step's duration, short."""
    if ms is None:
        return ""
    if ms < 1000:
        return f"{ms}ms"
    return f"{ms / 1000:.1f}s"


def line_text(line: Line, width: int = 0) -> Text:
    """One tool line: glyph, name, target, duration; an error in red with its reason."""
    text = Text("  ")
    if line.status == "saved":
        text.append("▤ ", style=ACCENT)
        text.append("saved".ljust(18), style=MUTED)
        text.append(line.target, style=ACCENT)
        return text
    glyph, colour = {"running": ("●", BLUE), "ok": ("✓", GREEN), "error": ("✗", RED)}[line.status]
    text.append(f"{glyph} ", style=colour)
    text.append(line.name.ljust(18), style="bold" if line.status != "error" else f"bold {RED}")
    text.append(line.target, style="" if line.status != "error" else RED)
    if line.status == "error":
        text.append(f"  refused {line.code}", style=f"bold {RED}")
        if line.reason:
            text.append(f": {line.reason}", style=RED)
    if line.duration_ms is not None:
        text.append(f"  {duration(line.duration_ms)}", style=MUTED)
    return text


def turn_renderable(turn: AgentTurn, *, expanded: bool = True, max_lines: int = 12) -> Group:
    """An agent turn: a header, the model's text, then one unwrapped line per tool call."""
    head = Text()
    head.append(f"{turn.agent or 'agent'}", style=f"bold {BLUE}")
    head.append(f"  {turn.instance}", style="bold")
    calls = sum(1 for ln in turn.lines if ln.status != "saved")
    failed = sum(1 for ln in turn.lines if ln.status == "error")
    meta = f"  {turn.run} · {plural(turn.turns, 'turn')} · {plural(calls, 'call')}"
    head.append(meta, style=MUTED)
    if failed:
        head.append(f" · {failed} refused", style=RED)
    parts: list[RenderableType] = [head]
    parts.extend(cited(para) for para in turn.text)
    lines = turn.lines
    hidden = 0
    if not expanded and len(lines) > max_lines:
        keep = [ln for ln in lines if ln.status in ("error", "saved", "running")]
        rest = [ln for ln in lines if ln.status not in ("error", "saved", "running")]
        room = max(max_lines - len(keep), 2)
        shown = set(map(id, keep + rest[-room:]))
        hidden = len(lines) - len(shown)
        lines = [ln for ln in lines if id(ln) in shown]
    if hidden:
        parts.append(Text(f"  … {hidden} earlier calls (click to show)", style=MUTED))
    for line in lines:
        text = line_text(line)
        text.no_wrap = True
        text.overflow = "ellipsis"
        parts.append(text)
    if turn.citations:
        refs = Text("  ")
        for c in turn.citations:
            refs.append(f"[{c.n}]", style=f"bold {ACCENT}")
            refs.append(f" {c.record or c.span}  ", style=MUTED)
        parts.append(refs)
    return Group(*parts)


def plural(n: int, word: str) -> str:
    """``1 call``, ``2 calls``."""
    return f"{n} {word}{'' if n == 1 else 's'}"


def cited(text: str) -> Text:
    """Model text with its ``[n]`` citations highlighted."""
    out = Text()
    for part in _split_citations(text):
        style = f"bold {ACCENT}" if part.startswith("[") and part[1:-1].isdigit() else ""
        out.append(part, style=style)
    return out


def _split_citations(text: str) -> list[str]:
    parts: list[str] = []
    buf = ""
    i = 0
    while i < len(text):
        if text[i] == "[":
            j = text.find("]", i)
            if j > i + 1 and text[i + 1 : j].isdigit():
                parts.append(buf)
                parts.append(text[i : j + 1])
                buf = ""
                i = j + 1
                continue
        buf += text[i]
        i += 1
    parts.append(buf)
    return [p for p in parts if p]


def item_renderable(item: Item, *, expanded: bool = False) -> RenderableType:
    """Any transcript item as Rich text."""
    if isinstance(item, UserMessage):
        text = Text.assemble(("you", "bold"))
        if item.agent:
            text.append(f"  to {item.agent}", style=MUTED)
        text.append("\n")
        text.append(item.text)
        return text
    if isinstance(item, AgentTurn):
        return turn_renderable(item, expanded=expanded)
    if isinstance(item, Question):
        return question_summary(item)
    if isinstance(item, Approval):
        return approval_summary(item)
    if isinstance(item, Outcome):
        word = {"complete": "complete"}.get(item.outcome, item.outcome)
        text = status_text(
            item.outcome if item.outcome in STATUS else "failed",
            f"{item.run} {item.agent or ''} ended {word}",
        )
        if item.reason:
            text.append(f"  ({item.reason})", style=MUTED)
        return text
    if isinstance(item, Notice):
        colour = TONES.get(item.tone, MUTED)
        text = Text.assemble((f"{item.glyph} ", colour))
        text.append(item.text, style=colour if item.tone != "info" else MUTED)
        return text
    return refusal_text(item)


def refusal_text(item: Refusal) -> Text:
    """A refused command: its name, code and message."""
    text = Text.assemble(("✗ refused ", f"bold {RED}"))
    text.append(f"{item.command} {item.code}", style=RED)
    if item.message:
        text.append(f": {item.message}", style=RED)
    if item.code == "lane_required":
        text.append("  (a run is open: Tab picks steer, follow-up or amend)", style=MUTED)
    return text


def question_summary(q: Question) -> Text:
    """A question in the thread: open (amber), answered (green) or voided."""
    glyph, colour = {"open": ("◆", AMBER), "resolved": ("✓", GREEN), "voided": ("○", MUTED)}[
        q.state
    ]
    text = Text.assemble((f"{glyph} {q.qkind} ", f"bold {colour}"))
    text.append(f"{q.id} · {q.label}", style=colour)
    for _, asked in q.questions:
        text.append(f"\n  {asked}")
    if q.state == "resolved":
        for answer in q.answers.values():
            text.append("\n  → ", style=GREEN)
            text.append(answer, style="bold")
    elif q.state == "voided":
        text.append(f"\n  voided: {q.reason}", style=MUTED)
    else:
        text.append("\n  waiting on you", style=AMBER)
    return text


def approval_summary(a: Approval) -> Text:
    """An approval in the thread, with its decision."""
    glyph, colour, word = {
        "open": ("◆", AMBER, "waiting on you"),
        "granted": ("✓", GREEN, "approved"),
        "rejected": ("✗", RED, "rejected"),
        "voided": ("○", MUTED, "voided"),
    }[a.state]
    text = Text.assemble((f"{glyph} approval ", f"bold {colour}"))
    text.append(f"{a.id} · {a.tool}", style=colour)
    text.append("  ▲ leaves Aksha" if a.state == "open" else "", style=RED)
    for name, value in a.facts:
        text.append(f"\n  {name:<12}", style=MUTED)
        text.append(value)
    text.append(f"\n  {word}", style=colour)
    if a.by and a.state != "open":
        text.append(f" by {a.by}", style=MUTED)
    if a.note:
        text.append(f": {a.note}", style=MUTED)
    return text


# --- opened views (the drawer) ---------------------------------------------------------------


def view_kind(content: Any, media_type: str | None = None) -> str:
    """Which renderer an opened thing gets: ``table``, ``json``, ``markdown`` or ``text``."""
    if isinstance(content, list) and content and all(isinstance(r, Mapping) for r in content):
        return "table"
    if isinstance(content, Mapping) and _rows(content) is not None:
        return "table"
    if isinstance(content, Mapping | list):
        return "json"
    if media_type in ("text/csv",) or (media_type or "").endswith("csv"):
        return "csv"
    if media_type in ("text/markdown",) or (
        isinstance(content, str) and content.lstrip()[:2] in ("# ", "##")
    ):
        return "markdown"
    return "text"


def _rows(content: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    for key in ("rows", "items", "candidates", "entries"):
        rows = content.get(key)
        if isinstance(rows, list) and rows and all(isinstance(r, Mapping) for r in rows):
            return rows
    return None


def view_renderable(content: Any, kind: str) -> RenderableType:
    """``content`` drawn as ``kind`` (see :func:`view_kind`)."""
    if kind == "table":
        rows = content if isinstance(content, list) else _rows(content) or []
        head = Text(f"{len(rows)} rows", style=MUTED)
        if isinstance(content, Mapping):
            for key, value in content.items():
                if isinstance(value, str | int | float) and not isinstance(value, bool):
                    head.append(f"\n{key}: ", style=MUTED)
                    head.append(str(value))
        return Group(head, rows_table(rows))
    if kind == "json":
        return Syntax(
            json.dumps(content, indent=2, ensure_ascii=False),
            "json",
            theme="ansi_dark",
            word_wrap=True,
        )
    if kind == "csv":
        import csv
        import io

        reader = list(csv.reader(io.StringIO(str(content))))
        if reader:
            return rows_table([dict(zip(reader[0], r, strict=False)) for r in reader[1:]])
    if kind == "markdown":
        return Markdown(str(content))
    return Text(str(content))


def rows_table(rows: Sequence[Mapping[str, Any]]) -> Table:
    """Rows of objects as a table, columns in first-seen order."""
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(str(key))
    table = Table(show_lines=False, header_style=f"bold {MUTED}", border_style=MUTED, expand=True)
    for column in columns:
        longest = max([len(column), *(len(_cell(r.get(column))) for r in rows)])
        table.add_column(column, overflow="ellipsis", no_wrap=True, ratio=max(min(longest, 30), 4))
    for row in rows:
        table.add_row(*[_cell(row.get(c)) for c in columns])
    return table


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def as_text(content: Any) -> str:
    """Content as text lines for a diff: JSON pretty-printed with sorted keys."""
    if isinstance(content, str):
        return content
    return json.dumps(content, indent=2, sort_keys=True, ensure_ascii=False)


def diff_renderable(before: Any, after: Any, old: str, new: str) -> Group:
    """A unified diff of two versions: added lines green, removed red, with a count."""
    lines = list(
        difflib.unified_diff(
            as_text(before).splitlines(),
            as_text(after).splitlines(),
            fromfile=old,
            tofile=new,
            lineterm="",
            n=2,
        )
    )
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    head = Text.assemble((f"{old} → {new}  ", "bold"))
    head.append(f"+{added}", style=f"bold {GREEN}")
    head.append(" ")
    head.append(f"-{removed}", style=f"bold {RED}")
    body = Text()
    if not lines:
        body.append("no change", style=MUTED)
    for ln in lines[2:]:
        if ln.startswith("+"):
            body.append(ln + "\n", style=f"{GREEN} on #16301f")
        elif ln.startswith("-"):
            body.append(ln + "\n", style=f"{RED} on #3a1a1a")
        elif ln.startswith("@@"):
            body.append(ln + "\n", style=MUTED)
        else:
            body.append(ln + "\n")
    return Group(head, Text(""), body)
