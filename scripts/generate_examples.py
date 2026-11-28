"""Regenerate ``examples/``: real offline runs of both agents through the command line.

Every file under ``examples/`` is written by this script, never by hand. It drives the
``aksha`` command exactly as a person would type it, on the synthetic ``stillroom`` world,
with the rule-based fake models (``AKSHA_MODE=fake``, the default), the world's offline web
(``AKSHA_SEARCH=world:stillroom``) and the mock counsel mailbox. Nothing calls a network.

    .venv/bin/python scripts/generate_examples.py            # rewrite examples/
    .venv/bin/python scripts/generate_examples.py --out DIR  # write somewhere else

Each command's output is copied verbatim, with three substitutions so a rerun gives a
stable diff: the scratch folder becomes ``<work>``, a random session ID becomes
``S-<session>``, and the demo's timings become ``(n s)``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CLI = [sys.executable, "-m", "aksha.interfaces.cli"]

RESEARCH_REQUEST = (
    "Who sells camera-free occupancy sensing, and at what price? Compare power (PoE, "
    "Ethernet, mains, USB-C, battery powered, battery thermal, AA), battery life (battery "
    "life, years of battery, months at) and privacy (privacy, camera, faces, identities, "
    "image). What do customers say about: battery (batter*, not battery powered); stillness "
    "(sit*, sat, seated, still, do not move, barely move); setup (cabl*, electrician, "
    "socket*, wiring, install*, setup); privacy (privacy, works council, image)?"
)
PATENT_REQUEST = (
    "Which of these inventions should we take to a patent attorney, and why? Prepare the "
    "selected disclosure documents."
)
VENT_ANSWER = "0.5 m from the vent edge, so its air flow stays outside the footprint."
ROUTED_RESEARCH = (
    "Who sells occupancy sensors for offices in the EU, and at what price? What do customers "
    "say about: battery (batter*); setup (install*, cabl*, electrician)?"
)
UNCLEAR = "Help me with our sensor."


# --- running commands and keeping a transcript ------------------------------------------


@dataclass
class Step:
    """One command in a transcript: why it was typed, what it printed, its exit code."""

    note: str
    shown: str
    output: str
    code: int
    detail: str = ""


@dataclass
class Session:
    """One scratch AKSHA_HOME and the transcript of the commands run in it."""

    work: Path
    env: dict[str, str]
    steps: list[Step] = field(default_factory=list)

    def run(self, note: str, args: Sequence[str], shown_env: str = "") -> str:
        """Run one ``aksha`` command in this session and keep it in the transcript."""
        proc = subprocess.run(
            [*CLI, *args],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        out = clean(proc.stdout + proc.stderr, self.work)
        shown = " ".join(["aksha", *(quote(a) for a in args)])
        self.steps.append(Step(note, f"{shown_env}{shown}", out, proc.returncode))
        return out

    def transcript(self, title: str, intro: str, setup: str) -> str:
        """The commands so far as a Markdown transcript."""
        lines = [f"# {title}", "", intro, "", "## Setup", "", "```sh", setup, "```", ""]
        for i, s in enumerate(self.steps, start=1):
            lines += [
                f"## {i}. {s.note}",
                "",
                *([s.detail, ""] if s.detail else []),
                "```console",
                f"$ {s.shown}",
                s.output.rstrip(),
                f"(exit code {s.code})",
                "```",
                "",
            ]
        return "\n".join(lines)


def quote(arg: str) -> str:
    """An argument as a person would type it in a shell."""
    return arg if re.fullmatch(r"[\w@%+=:,./~-]+", arg) else shlex.quote(arg)


def clean(text: str, work: Path) -> str:
    """The output with the scratch folder, session IDs and timings made stable."""
    text = text.replace(str(work), "<work>")
    text = re.sub(r"\bS-[0-9a-f]{12}\b", "S-<session>", text)
    return re.sub(r"\(\d+(\.\d+)? s\)", "(n s)", text)


def ident(pattern: str, text: str) -> str:
    """The first group of ``pattern`` in ``text``; stop with the output if absent."""
    found = re.search(pattern, text)
    if found is None:
        raise SystemExit(f"expected {pattern!r} in the output:\n{text}")
    return found.group(1)


def make_session(base: Path, name: str) -> Session:
    """A fresh scratch AKSHA_HOME with the offline web and the mock mailbox."""
    work = base / name
    work.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AKSHA_")}
    env.update(
        AKSHA_HOME=str(work / "home"),
        AKSHA_SEARCH="world:stillroom",
        AKSHA_SYSTEMS=f"counsel_mail=mailbox:{work / 'mock-counsel'}",
        AKSHA_MODE="fake",
    )
    return Session(work, env)


def show(s: Session, ref: str) -> dict[str, Any]:
    """One output as ``aksha show`` prints it, parsed."""
    proc = subprocess.run(
        [*CLI, "show", ref], cwd=s.work, env=s.env, capture_output=True, text=True, check=True
    )
    data: dict[str, Any] = json.loads(proc.stdout)
    return data


def latest_refs(s: Session) -> list[str]:
    """Every latest output reference ``aksha show`` lists."""
    proc = subprocess.run(
        [*CLI, "show"], cwd=s.work, env=s.env, capture_output=True, text=True, check=True
    )
    return [line.split()[0] for line in proc.stdout.splitlines() if "@v" in line]


SETUP = """make venv && . .venv/bin/activate
export AKSHA_HOME=var                 # this walk-through's state stays in ./var
export AKSHA_SEARCH=world:stillroom   # the world's offline web: vendor pages and publications
export AKSHA_SYSTEMS=counsel_mail=mailbox:var/mock-counsel   # the mock counsel mailbox
# AKSHA_MODE=fake is the default: rule-based fake models, no keys, no network"""


# --- the three conversations ------------------------------------------------------------


def research_and_patent(base: Path) -> Session:
    """Both agents in one store, as the two conversations show them."""
    s = make_session(base, "agents")
    s.run(
        "Copy the synthetic stillroom inputs to a folder of your own",
        ["example", "stillroom", "inputs"],
    )
    out = s.run(
        "Ask the research agent; it asks only the questions that change the result",
        ["run", "--agent", "research", "--input", "inputs", RESEARCH_REQUEST],
    )
    q = ident(r"waiting for input \(question\) (I-\d+)", out)
    s.run(
        "Answer both questions; the run finishes",
        ["answer", q, "--answer", "segment=offices", "--answer", "geography=EU"],
    )
    s.run("List what it saved, each output with its version", ["show"])
    s.run("Print one output: the competitor matrix, every cell cited", ["show", "research.matrix"])
    out = s.run(
        "Ask the patent agent; it finds a detail the engineering note leaves open",
        ["run", "--agent", "patent", PATENT_REQUEST],
    )
    gap = ident(r"waiting for input \(missing detail\) (I-\d+)", out)
    out = s.run(
        "Give the missing detail; it recommends a next action for all four candidates "
        "and asks which to prepare",
        ["answer", gap, VENT_ANSWER],
    )
    choice = ident(r"waiting for choice \(candidate selection\) (I-\d+)", out)
    out = s.run(
        "Choose C1; the selection is saved, the forms and packet are filled, and the "
        "send waits for its own approval",
        ["answer", choice, "C1"],
    )
    approval = ident(r"waiting for approval (A-\d+)", out)
    signature = ident(r"--signature (v1:[0-9a-f]{12})", out)
    s.run(
        "Approve the exact send shown; the mock mailbox receives the packet once",
        ["approve", approval, "--signature", signature],
    )
    s.run("Approving again delivers nothing", ["approve", approval, "--signature", signature])
    s.run(
        "The recommendation for C1, with its rule, risks and what would change it",
        ["show", "patent.recommendation/C1"],
    )
    s.run("Where every run stands, read from the event store", ["status"])
    s.run("Check the event store's hash chain", ["verify"])
    return s


BUDGET_NOTE = (
    " Note: this routed run recorded no budget of its own on `run.started`, so it runs on the "
    "runtime default (`DEFAULT_BUDGET`, 40 tool calls) and holds on a budget interrupt the "
    "same request run with `--agent` does not reach."
)


def routing(base: Path) -> Session:
    """Requests with no ``--agent``: a route question, and a routed request."""
    s = make_session(base, "routing")
    s.run("Copy the inputs", ["example", "stillroom", "inputs"])
    out = s.run(
        "An unclear request with no agent named: the router asks which agent is meant",
        ["run", "--input", "inputs", UNCLEAR],
    )
    route = ident(r"waiting for route (I-\d+)", out)
    s.run(
        "Name the agent; the run starts on the patent agent and asks its first question",
        ["answer", route, "--agent", "patent"],
    )
    s.run(
        "A clear request with no --agent: the router picks the research agent from the "
        "agents' declared purposes, with no question",
        ["run", ROUTED_RESEARCH],
    )
    s.run("Each run line names the agent that handled it", ["status"])
    for st in s.steps:
        if "waiting for budget" in st.output and st.shown.split()[1] in ("run", "answer"):
            st.detail = BUDGET_NOTE.strip()
    return s


# --- outputs and readable summaries ------------------------------------------------------


def cite(e: Mapping[str, Any] | None) -> str:
    """One evidence item as `record`: "quote" (span), or the output path it points at."""
    if not e:
        return ""
    c = e.get("cite", e)
    if "artifact" in c:
        return f"`{c['artifact']}#{c.get('path', '')}`"
    where = e.get("record") or c.get("file") or e.get("file") or ""
    quote_ = " ".join(str(c.get("quote") or e.get("quote") or "").split())
    span = f" ({c['span']})" if c.get("span") else ""
    return f'{"`" + where + "`: " if where else ""}"{quote_}"{span}'


def value(v: Any) -> str:
    """A matrix entry or a price: the value as listed, or unknown with its reason."""
    if isinstance(v, dict):
        if v.get("status") == "unknown":
            return f"unknown ({v.get('reason')})"
        if "currency" in v:
            return f"{v['currency']} {v['number']} per {v['per']} ({v.get('terms', '')})"
        return str(v.get("value", ""))
    return str(v)


def cell(v: Any) -> str:
    """A value on one Markdown table line."""
    text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return " ".join(text.split()).replace("|", "\\|")


def table(head: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """A Markdown table."""
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(cell(x) for x in r) + " |" for r in rows]
    return [*out, ""]


def research_summary(c: Mapping[str, Mapping[str, Any]]) -> str:
    """The research agent's outputs as readable Markdown."""
    scope, matrix = c["research.scope"], c["research.matrix"]
    lines = [
        "# Research agent: readable summary",
        "",
        "Generated by `scripts/generate_examples.py` from the JSON beside it. Every value "
        "below is copied from a saved output; the JSON keeps the full citations.",
        "",
        "## Scope",
        "",
    ]
    lines += [
        f"- Segment: {scope['segment'].get('value')} ({scope['segment'].get('basis')})",
        f"- Geography: {scope['geography'].get('value')} ({scope['geography'].get('basis')})",
        f"- In bounds: {scope['boundary']['in']}",
        f"- Out of bounds: {'; '.join(value(x) for x in scope['boundary']['out'])}",
        "",
    ]
    lines += ["## Competitor matrix (`research.matrix`)", ""]
    dims = [d["id"] for d in matrix["dimensions"]]
    rows = []
    for r in matrix["rows"]:
        prices = "; ".join(value(p) for p in r["prices"])
        if r.get("price_conflict"):
            prices += " (conflicting prices kept visible)"
        cells = ["; ".join(value(e) for e in r["cells"].get(d, [])) or "unknown" for d in dims]
        rows.append([r["company"].get("value", r["key"]), prices or "unknown", *cells])
    lines += table(["Company", "Prices", *dims], rows)
    lines += [matrix["note"], ""]
    web = c["research.competitors"]["web"]
    lines += [
        "## Sources (`research.competitors`)",
        "",
        "Pages the agent fetched from the offline web. A claim cites a span (EX-...) read "
        "from one of these pages, never a search snippet.",
        "",
    ]
    lines += [f"- fetched: {u}" for u in web["fetched"]]
    lines += [f"- skipped: {x['url']} ({x['why']})" for x in web["skipped"]]
    lines += ["", "## Findings (`research.findings`)", ""]
    for f in c["research.findings"]["findings"]:
        lines.append(f"- **{f['id']}** ({f['label']}): {f['statement']}")
        lines += [f"  - source: {cite(e)}" for e in f["evidence"][:3]]
    lines += ["", "Unknowns, kept as questions rather than filled in:", ""]
    lines += [f"- {u['question']} ({u['reason']})" for u in c["research.findings"]["unknowns"]]
    needs = c["research.needs"]
    lines += [
        "",
        "## Customer needs (`research.needs`)",
        "",
        f"Sample: {needs['sample']['rows']} review rows, {needs['sample']['unique']} "
        f"after removing duplicates. {needs['sample']['note']}",
        "",
    ]
    for n in needs["needs"]:
        lines.append(f"- **{n.get('theme')}**: {n.get('need')} ({n.get('count')} reviews)")
        lines += [f"  - source: {cite(e)}" for e in n.get("evidence", [])[:2]]
    lines += ["", "## Proposed customer tests (`research.tests`)", ""]
    for t in c["research.tests"]["tests"]:
        lines += [
            f"- **{t['id']}** ({t['theme']}): {t['hypothesis']}",
            f"  - method: {t['method']}",
            f"  - metric: {t['metric']}",
            f"  - threshold: {t['threshold']['value']} (status "
            f"{t['threshold']['status']}: not a measured result)",
        ]
    return "\n".join(lines) + "\n"


def patent_summary(c: Mapping[str, Mapping[str, Any]]) -> str:
    """The patent agent's outputs as readable Markdown."""
    lines = [
        "# Patent agent: readable summary",
        "",
        "Generated by `scripts/generate_examples.py` from the JSON beside it. The agent "
        "orders work for counsel; it gives no patentability or freedom-to-operate view.",
        "",
        "## Recommendation table (`patent.table`)",
        "",
    ]
    lines += table(
        ["Candidate", "Next action", "Why", "Would change if"],
        [
            [r["candidate"], r["next_action"], r["why"], r["would_change_if"]]
            for r in c["patent.table"]["rows"]
        ],
    )
    lines += [c["patent.table"]["note"], ""]
    for key in sorted(k for k in c if k.startswith("patent.recommendation/")):
        r = c[key]
        lines += [f"- **{r['candidate']}** risks: " + ("; ".join(r["risks"]) or "none recorded")]
    sel = c["patent.selection"]
    lines += [
        "",
        "## Saved selection (`patent.selection`)",
        "",
        f"Selected {', '.join(sel['selected'])}, chosen by {sel['chosen_by']}, from "
        f"{sel['table']}.",
        "",
    ]
    for cand in sel["selected"]:
        comp = c[f"patent.comparison/{cand}"]
        lines += [
            f"## Comparison matrix for {cand} (`patent.comparison/{cand}`)",
            "",
            comp["note"],
            "",
        ]
        lines += table(
            ["Element", "Family", "State", "Members"],
            [
                [x["element"], x["family"], x["state"], ", ".join(x["members"])]
                for x in comp["cells"]
            ],
        )
        for key in sorted(k for k in c if k.startswith(f"patent.form/{cand}~")):
            form = c[key]
            n = form["counts"]
            lines += [
                f"## {form['form']} (`{key}`)",
                "",
                f"{n['complete']} complete, {n['missing']} missing, "
                f"{n['needs_counsel_confirmation']} need counsel confirmation.",
                "",
            ]
            lines += table(
                ["Field", "Status", "Value", "Source"],
                [
                    [
                        f["field"],
                        f["status"],
                        ("(empty)" if f.get("value") is None else cell(f["value"]))[:200],
                        f.get("source", ""),
                    ]
                    for f in form["fields"]
                ],
            )
        pkt = c[f"patent.packet/{cand}"]
        lines += [f"## Counsel-review packet (`patent.packet/{cand}`)", ""]
        for sec in pkt["sections"]:
            lines += [f"### {sec['title']}", ""]
            if "text" in sec:
                lines += [sec["text"], ""]
            for q in sec.get("questions", []):
                lines.append(
                    f"- question for counsel: {q.get('question', q) if isinstance(q, dict) else q}"
                )
            for it in sec.get("items", []):
                lines.append(f"- `{it['field']}` ({it['form'].split('/')[-1]}): {it['question']}")
            if "counts" in sec:
                n = sec["counts"]
                lines.append(
                    f"- {n['complete']} fields complete, {n['missing']} missing, "
                    f"{n['needs_counsel_confirmation']} need counsel confirmation"
                )
                lines += [
                    f"- missing: {m.get('field', m) if isinstance(m, dict) else m}"
                    for m in sec.get("missing", [])
                ]
            if "comparison" in sec:
                lines.append(
                    f"- from `{sec['comparison']}`; closest families: "
                    f"{', '.join(sec['closest_families'])}"
                )
            if "recommendation" in sec:
                lines.append(
                    f"- `{sec['next_action']}` from `{sec['recommendation']}`: {sec['why']}"
                )
                lines += [f"- risk: {r}" for r in sec["risks"]]
            for o in sec.get("outside_contributions", []):
                lines.append(
                    f"- outside contributor {o['person_id']} ({o['date']}, elements "
                    f"{', '.join(o['elements'])}): `{o['status']}`; {o['counsel_question']}"
                )
            if "contributors" in sec:
                lines.append(f"- {len(sec['contributors'])} contributors recorded")
            lines.append("")
        h = c.get(f"patent.handoff/{cand}")
        if h:
            lines += [
                "",
                f"Handoff: {h['packet']} to {h['recipient']}, status {h['status']} "
                f"({h['receipt']}), after a separate approval.",
                "",
            ]
    return "\n".join(lines) + "\n"


def save_outputs(s: Session, out: Path) -> dict[str, dict[str, Any]]:
    """Write every latest output as JSON, then the readable summaries."""
    folder = out / "outputs"
    folder.mkdir(parents=True, exist_ok=True)
    contents: dict[str, dict[str, Any]] = {}
    for ref in latest_refs(s):
        slot = ref.split("@", 1)[0]
        data = show(s, slot)
        contents[slot] = data["content"]
        name = slot.replace("/", "__").replace("~", "--") + ".json"
        (folder / name).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    write_summaries(folder, contents)
    return contents


def write_summaries(folder: Path, contents: Mapping[str, Mapping[str, Any]]) -> None:
    """Write both readable summaries from the outputs' contents."""
    (folder / "research-summary.md").write_text(research_summary(contents))
    (folder / "patent-summary.md").write_text(patent_summary(contents))


# --- the change-and-recovery walkthrough -------------------------------------------------


def artifact(db: Path, kind: str, key: str | None, ver: int) -> dict[str, Any]:
    """One saved output version from the demo's store, read-only."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    row = con.execute(
        "SELECT content FROM artifacts WHERE kind=? AND key IS ? AND version=?", (kind, key, ver)
    ).fetchone()
    con.close()
    if row is None or row[0] is None:
        raise SystemExit(f"no {kind}/{key}@v{ver} in the demo store")
    content: dict[str, Any] = json.loads(row[0])
    return content


def walkthrough(base: Path) -> str:
    """Run ``aksha demo`` and annotate its acts in domain language."""
    s = make_session(base, "demo")
    report_path = s.work / "demo-report.json"
    out = s.run("Run the demo", ["demo", "--work", "demo", "--json", "demo-report.json"])
    report = json.loads(report_path.read_text())
    db = s.work / "demo" / "aksha.sqlite"
    ext = [artifact(db, "patent.extraction", "C1", v)["outside_contributions"] for v in (1, 2)]
    rec = [artifact(db, "patent.recommendation", "C1", v) for v in (1, 2)]
    sel = artifact(db, "patent.selection", None, 2)
    sections = re.split(r"\n(?=\d\. )", out.strip())
    head, acts = sections[0], {s_[0]: s_ for s_ in sections[1:]}
    o1, o2 = ext[0][0], ext[1][0]
    a1, a2 = report["approvals"][0], report["approvals"][-1]
    lines = [
        "# Change and recovery, step by step",
        "",
        "Generated by `scripts/generate_examples.py` from a real run of `aksha demo` "
        "([src/aksha/interfaces/demo/](../src/aksha/interfaces/demo/), tested by "
        "[tests/integration/test_demo.py](../tests/integration/test_demo.py)). The demo's "
        "output is quoted in full, one act at a time, with what each act means for the venture.",
        "",
        "```console",
        "$ aksha demo",
        head.rstrip(),
        "```",
        "",
        "## 1. The venture's first pass",
        "",
        "Both agents run on the synthetic stillroom world. The research agent asks which "
        "geography to cover; the patent agent asks the inventor one missing detail, then asks "
        "which candidate to prepare. C1 (self-calibration) is chosen, its forms and "
        "counsel-review packet are filled, and the packet goes to the mock counsel mailbox only "
        f"after approval {a1['approval']} for {a1['targets'][0]}.",
        "",
        "```console",
        acts["1"].rstrip(),
        "```",
        "",
        "## 2. New facts arrive",
        "",
        "Two things change. A closer prior-art publication appears (a new family). The outside "
        "consultant's email that suggested part of C1's mechanism turns out to be dated "
        f"earlier: {o1['date']} before, {o2['date']} after. That date now falls before the "
        "consultant's agreement took effect.",
        "",
        "```console",
        acts["2"].rstrip(),
        "```",
        "",
        "## 3. Only the affected work reopens",
        "",
        "Invalidation follows what each output actually read. Everything built on C1's "
        "extraction and prior art is rebuilt: the comparison, the recommendation, both forms, "
        "the packet and its handoff. C2, C3 and C4 keep their extraction, prior art and "
        "comparison, and 7 of the 11 research outputs are kept. The recommendations of C2, "
        "C3 and C4 reopen because each reads the research needs and findings, which moved, "
        "and their next actions come out unchanged.",
        "",
        f"C1's next action moves from `{rec[0]['next_action']}` to `{rec[1]['next_action']}`. "
        "The recommendation explains why with the new evidence:",
        "",
        f"- before: {rec[0]['why']}",
        f"- after: {rec[1]['why']}",
        f"- risks after: {'; '.join(rec[1]['risks'])}",
        "",
        f"The external contributor ({o2['person_id']}) is flagged for counsel with status "
        f"`{o2['status']}`. The question changes with the new date:",
        "",
        f'- before: "{o1["counsel_question"]}"',
        f'- after: "{o2["counsel_question"]}"',
        "",
        f"Approval {a1['approval']} named {a1['targets'][0]} exactly, so it cannot cover the "
        f"rebuilt {a2['targets'][0]}. The packet's diff review is accepted and a fresh "
        f"approval, {a2['approval']}, is requested and granted for the new version.",
        "",
        "```console",
        acts["3"].rstrip(),
        "```",
        "",
        "## 4. A crash in the middle of the send",
        "",
        "A child process sends the new packet and is killed with a real SIGKILL after the "
        "mailbox stores it and before the receipt is recorded. On restart, recovery asks the "
        "mailbox by idempotency key instead of sending again. The mailbox ends with one "
        "delivery per packet version and no duplicate.",
        "",
        "```console",
        acts["4"].rstrip(),
        "```",
        "",
        "## 5. The record is intact",
        "",
        "```console",
        acts["5"].rstrip(),
        "```",
        "",
        "## What the new evidence did to the shortlist",
        "",
        f"C1 was the one candidate marked `{rec[0]['next_action']}`. With a closer reference "
        f"and an ownership question open, it is now `{rec[1]['next_action']}`. The saved "
        f"selection is rebuilt from the new recommendations and still holds "
        f"{', '.join(sel['selected'])}, as the person chose ({sel['chosen_by']}). Its packet "
        "goes to counsel with the contributor and ownership question marked for their "
        "confirmation, since inventorship and ownership are counsel's decision, not the "
        "agent's. The full report "
        "the demo wrote is in [outputs/demo-report.json](outputs/demo-report.json).",
        "",
    ]
    (base / "demo-report.json").write_text(clean(report_path.read_text(), s.work))
    return "\n".join(lines)


# --- main --------------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Regenerate the examples folder."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, default=ROOT / "examples")
    parser.add_argument(
        "--summaries",
        action="store_true",
        help="only rewrite the readable summaries from the saved JSON",
    )
    args = parser.parse_args(argv)
    out: Path = args.out
    if args.summaries:
        write_summaries(
            out / "outputs",
            {
                json.loads(f.read_text())["ref"].split("@", 1)[0]: json.loads(f.read_text())[
                    "content"
                ]
                for f in sorted((out / "outputs").glob("*.*.json"))
            },
        )
        return 0
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aksha-examples-") as tmp:
        base = Path(tmp)
        agents = research_and_patent(base)
        split = next(i for i, st in enumerate(agents.steps) if "patent" in st.shown.split()[:4])
        research = Session(agents.work, agents.env, agents.steps[:split])
        patent = Session(agents.work, agents.env, agents.steps[split:])
        (out / "research-conversation.md").write_text(
            research.transcript(
                "Research agent: an example conversation",
                "A real offline run on the synthetic stillroom world, through the command line. "
                "The request names the comparison dimensions and the review themes; the agent asks "
                "for the buyer segment and the geography because both change which competitors "
                "and reviews count. The outputs are in [outputs/](outputs/), summarised in "
                "[outputs/research-summary.md](outputs/research-summary.md).",
                SETUP,
            )
        )
        (out / "patent-conversation.md").write_text(
            patent.transcript(
                "Patent agent: an example conversation",
                "Continues in the same store after the research conversation. The agent asks for "
                "one detail the engineering note leaves open, recommends a next action for all "
                "four candidates, saves the person's selection, fills the forms and packet, "
                "and sends only after a separate approval of the exact action. The outputs are "
                "summarised in [outputs/patent-summary.md](outputs/patent-summary.md).",
                SETUP,
            )
        )
        save_outputs(agents, out)
        routed = routing(base)
        (out / "routing-conversation.md").write_text(
            routed.transcript(
                "Routing: a request with no agent named",
                "Without `--agent`, the router matches the request against each loaded agent's "
                "declared purpose. An unclear request gets a route question; a clear one goes "
                "straight to its agent, and every run line names the agent.",
                SETUP,
            )
        )
        text = walkthrough(base)
        (out / "change-and-recovery.md").write_text(text)
        (out / "outputs" / "demo-report.json").write_text((base / "demo-report.json").read_text())
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
