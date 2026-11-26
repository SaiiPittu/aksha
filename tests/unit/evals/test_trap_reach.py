"""A planted-trap case is put to an agent that reads the trap's file (eval design 13).

``trap.T-INJECT`` reads ``handled`` only when the agent opened the page holding the
instruction, so a case that puts the trap to an agent that never reads that page can only
read ``missed``: it measures the case, not the agent. The injected instruction is on a
competitor page, which the research agent reads and the patent agent does not, so the
injection case lives in the research suite.
"""

from __future__ import annotations

import pytest

from aksha.agents.patent.world import COLLECTIONS as PATENT
from aksha.agents.research.tools import COLLECTIONS as RESEARCH
from aksha.evals.case import load_suite
from tests.support.evals import SUITES, key

READS = {"research": RESEARCH, "patent": PATENT}
"""The collections each runtime agent's tools read from."""


def _trap_files(trap_id: str) -> list[str]:
    trap = key().traps[trap_id]
    facts = [key().fact(f) for f in trap.facts]
    files = [str(f) for f in trap.raw.get("files", ())]
    return files + [f.anchor.file for f in facts if f.anchor is not None]


@pytest.mark.parametrize("suite", ["research", "patent"])
def test_every_planted_trap_is_on_a_page_the_agent_reads(suite: str) -> None:
    for c in load_suite(SUITES / suite).cases:
        if c.origin != "planted_trap" or c.trap_id != "T-INJECT":
            continue
        reads = READS[c.agents[0]]
        files = _trap_files(c.trap_id)
        assert files and all(f.split("/", 1)[0] in reads for f in files), (c.id, files)


def test_the_injection_case_is_in_the_research_suite() -> None:
    research = {c.id: c for c in load_suite(SUITES / "research").cases}
    assert research["s8a-injection-research"].trap_id == "T-INJECT"
    assert not [c.id for c in load_suite(SUITES / "patent").cases if c.trap_id == "T-INJECT"]
