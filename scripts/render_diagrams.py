"""Render every Mermaid diagram in the docs to a PNG, so it reads where Mermaid does not render.

The GitHub mobile app shows a ```mermaid block as source text. This script commits a
rendered image for each diagram and keeps the Mermaid source, collapsed, under it, so the
source stays the thing people edit and still renders on GitHub's website. It reads
``README.md``, ``docs/**/*.md`` and ``examples/*.md`` and turns each diagram into::

    <!-- diagram: docs/diagrams/<doc-slug>-<n>.png -->
    ![<nearest heading>](<path to the png, relative to the doc>)

    <details><summary>Diagram source (Mermaid)</summary>

    ```mermaid
    ...the source, unchanged...
    ```

    </details>

The source of each diagram is also written to ``docs/diagrams/src/<doc-slug>-<n>.mmd``.
Running it again changes nothing: a converted diagram is found by its wrapper and rendered
from its own block, and a PNG is rendered again only when its source changed (``--force``
renders them all). Images whose diagram is gone are removed.

    make diagrams                                 # the usual way
    python3 scripts/render_diagrams.py            # the same
    python3 scripts/render_diagrams.py --check    # fail if a doc or image is out of date

Rendering uses the official Mermaid command line tool through npx (Node.js and a Chrome or
Chromium browser are needed). ``MMDC`` overrides the command, ``PUPPETEER_EXECUTABLE_PATH``
picks the browser, and ``--puppeteer-config`` passes a puppeteer file (for example one with
``{"args": ["--no-sandbox"]}``). Only the standard library is used.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "docs" / "diagrams"
SRC_DIR = OUT_DIR / "src"
DEFAULT_MMDC = "npx -y @mermaid-js/mermaid-cli@11.17.1"
SUMMARY = "<details><summary>Diagram source (Mermaid)</summary>"

# A converted diagram (marker, image, details wrapper) or a bare ```mermaid block.
BLOCK = re.compile(
    r"(?P<wrapped><!-- diagram: [^\n]* -->\n!\[[^\n]*\]\([^\n)]*\)\n\n"
    + re.escape(SUMMARY)
    + r"\n\n```mermaid\n(?P<wsrc>.*?)\n```\n\n</details>)"
    r"|(?P<bare>^```mermaid\n(?P<bsrc>.*?)\n```$)",
    re.DOTALL | re.MULTILINE,
)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
TITLE = re.compile(r"\A---\n(?:.*\n)*?title:\s*(.+?)\s*\n(?:.*\n)*?---")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass
class Diagram:
    """One ```mermaid block: the doc it is in, its 1-based position there, its source."""

    doc: Path
    index: int
    source: str
    alt: str

    @property
    def name(self) -> str:
        """The file stem shared by the PNG and the .mmd copy."""
        return f"{slug(self.doc)}-{self.index}"

    @property
    def png(self) -> Path:
        """Where the rendered image goes."""
        return OUT_DIR / f"{self.name}.png"

    @property
    def mmd(self) -> Path:
        """Where the copy of the source goes."""
        return SRC_DIR / f"{self.name}.mmd"


def slug(doc: Path) -> str:
    """``docs/architecture/overview.md`` becomes ``architecture-overview``."""
    rel = doc.relative_to(ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[0] == "docs" and len(parts) > 1:
        parts = parts[1:]
    return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")


def docs() -> list[Path]:
    """The Markdown files whose diagrams are rendered."""
    found = [ROOT / "README.md", *ROOT.glob("docs/**/*.md"), *ROOT.glob("examples/*.md")]
    return sorted(p for p in found if p.is_file())


def alt_text(text: str, start: int, source: str) -> str:
    """The diagram's front-matter title, else the nearest heading above it."""
    title = TITLE.search(source)
    if title:
        alt = title.group(1)
    else:
        alt, fenced = "Diagram", False
        for line in text[:start].splitlines():
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
            elif not fenced and (h := HEADING.match(line)):
                alt = h.group(1)
    h1 = re.search(r"^# (.+)$", text, re.MULTILINE)
    if h1 and len(alt.split()) < 3 and alt != h1.group(1):
        # A bare "Decision" says little in an ADR: name the document too ("ADR-P02: Decision").
        alt = f"{re.split(r':| \(', h1.group(1))[0]}: {alt}"
    return re.sub(r"[`*_\[\]]", "", alt).strip() or "Diagram"


def wrap(d: Diagram) -> str:
    """The image, then the source collapsed under it."""
    rel_png = os.path.relpath(d.png, d.doc.parent).replace(os.sep, "/")
    rel_root = d.png.relative_to(ROOT).as_posix()
    return (
        f"<!-- diagram: {rel_root} -->\n![{d.alt}]({rel_png})\n\n{SUMMARY}\n\n"
        f"```mermaid\n{d.source}\n```\n\n</details>"
    )


def convert(doc: Path) -> tuple[str, list[Diagram]]:
    """Return the doc with every diagram wrapped, and the diagrams in order."""
    text = doc.read_text(encoding="utf-8")
    diagrams: list[Diagram] = []

    def replace(m: re.Match[str]) -> str:
        source = m.group("wsrc") if m.group("wrapped") else m.group("bsrc")
        d = Diagram(doc, len(diagrams) + 1, source, alt_text(text, m.start(), source))
        diagrams.append(d)
        return wrap(d)

    return BLOCK.sub(replace, text), diagrams


def png_size(path: Path) -> tuple[int, int]:
    """Width and height from the PNG header; ValueError if it is not a PNG."""
    head = path.read_bytes()[:24]
    if len(head) < 24 or head[:8] != PNG_MAGIC or head[12:16] != b"IHDR":
        raise ValueError(f"{path} is not a PNG")
    width, height = struct.unpack(">II", head[16:24])
    return width, height


def render(d: Diagram, mmdc: list[str], puppeteer: str | None) -> str | None:
    """Render one diagram; return an error message, or None on success."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.mmd"
        out = Path(tmp) / "out.png"
        src.write_text(d.source + "\n", encoding="utf-8")
        cmd = [*mmdc, "-i", str(src), "-o", str(out), "-b", "white", "-s", "2", "-w", "1600"]
        cmd += ["-q"] + (["-p", puppeteer] if puppeteer else [])
        run = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if run.returncode != 0 or not out.is_file():
            lines = (run.stderr or run.stdout).strip().splitlines()
            return " / ".join(lines[:4]) or f"exit code {run.returncode}"
        try:
            width, height = png_size(out)
        except ValueError as exc:
            return str(exc)
        if width < 100 or height < 40 or out.stat().st_size < 1000:
            return f"suspiciously small image ({width}x{height}, {out.stat().st_size} bytes)"
        d.png.write_bytes(out.read_bytes())
    return None


def main() -> int:
    """Convert the docs, render what changed, remove orphans; 1 on any failure."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="report what is out of date")
    parser.add_argument("--force", action="store_true", help="render every image again")
    parser.add_argument("--puppeteer-config", default=os.environ.get("MMDC_PUPPETEER_CONFIG"))
    args = parser.parse_args()
    mmdc = shlex.split(os.environ.get("MMDC", DEFAULT_MMDC))

    stale: list[str] = []
    failures: list[str] = []
    every: list[Diagram] = []
    for doc in docs():
        new_text, diagrams = convert(doc)
        every += diagrams
        rel = doc.relative_to(ROOT).as_posix()
        if new_text != doc.read_text(encoding="utf-8"):
            stale.append(rel)
            if not args.check:
                doc.write_text(new_text, encoding="utf-8")
        for d in diagrams:
            fresh = d.png.is_file() and d.mmd.is_file()
            fresh = fresh and d.mmd.read_text(encoding="utf-8") == d.source + "\n"
            if fresh and not args.force:
                continue
            if args.check:
                stale.append(d.png.relative_to(ROOT).as_posix())
                continue
            SRC_DIR.mkdir(parents=True, exist_ok=True)
            print(f"render {d.name} ({rel})", flush=True)
            error = render(d, mmdc, args.puppeteer_config)
            if error:
                failures.append(f"{rel} diagram {d.index}: {error}")
            else:
                d.mmd.write_text(d.source + "\n", encoding="utf-8")

    wanted = {d.png for d in every} | {d.mmd for d in every}
    orphans = [p for p in [*OUT_DIR.glob("*.png"), *SRC_DIR.glob("*.mmd")] if p not in wanted]
    for p in orphans:
        stale.append(p.relative_to(ROOT).as_posix())
        if not args.check:
            p.unlink()

    print(f"{len(every)} diagrams in {len({d.doc for d in every})} docs")
    for f in failures:
        print(f"FAILED {f}", file=sys.stderr)
    if args.check and stale:
        print("out of date (run make diagrams):\n  " + "\n  ".join(stale), file=sys.stderr)
        return 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
