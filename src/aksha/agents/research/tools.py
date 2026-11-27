"""The research agent's tools: the harness's shared input, web and plan tools, nothing of its own.

The agent reads one versioned source, :data:`SOURCE` (``inputs``), whose one collection
:data:`COLLECTION` (``input``) holds whatever the person uploaded or configured: saved web
pages, Markdown notes, CSV or JSON exports, emails, text extracted from a PDF, a slide deck
or a spreadsheet. It reads them with the harness's input tools (``list_inputs``,
``read_document``, ``search_inputs``) and the web with ``web_search`` and ``fetch_url``,
which shows a fetched page exactly as ``read_document`` shows an input. What it declares is
data: :data:`READING`, its source and its document kinds (``reading.KINDS``), so
``list_inputs`` names each input's kind and ``read_document(extract=true)`` fills that
kind's typed fields, each value cited to the words that state it.

Listings, searches and snippets are never citable: a claim needs the unit read. Every page
of a long export is read the same way (``offset``, ``limit``), so counting runs over the
whole export, never over what fits in one result.
"""

from __future__ import annotations

from aksha.runtime.api import (
    InputReading,
    RetryPolicy,
    fetch_url_tool,
    input_tools,
    todo_tool,
    web_search_tool,
)

from . import reading

AGENT = "research"
SOURCE = "inputs"
"""The versioned source the agent reads: the person's uploaded or configured inputs."""
COLLECTION = "input"
COLLECTIONS = (COLLECTION,)

READING = InputReading(
    SOURCE,
    COLLECTION,
    decide=reading.DECIDE,
    kinds=reading.KINDS,
    page_lines=600,
    page_chars=16_000,
    result_chars=24_000,
    retry=RetryPolicy(timeout_ms=600_000, max_retries=1),
)
"""How the shared input tools read for this agent: a window fits its 24,000-character result cap."""
ALIASES = READING.aliases()
"""The fast-class aliases the input tools ask: the decide alias and each kind's schema."""

TOOLS = (
    web_search_tool(AGENT),
    fetch_url_tool(AGENT, READING),
    *input_tools(AGENT, READING),
    todo_tool(AGENT),
)
