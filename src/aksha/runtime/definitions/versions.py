"""The extension API version, and ``requires_api`` specifiers such as ``">=3.0,<4"`` (D2).

``API_VERSION`` is ``major.minor``: adding a public name bumps the minor version; removing
or changing one bumps the major. :mod:`aksha.runtime.api` re-exports it.

A specifier is comma-separated clauses, each an operator (``>=``, ``>``, ``<=``, ``<``,
``==``) and a ``major[.minor]`` version. A missing minor reads as 0.
"""

from __future__ import annotations

import re

API_VERSION = "3.1"
"""The version of :mod:`aksha.runtime.api`; the snapshot test pins its names to it.

1.0 is the first frozen snapshot (ADR-P20 S1). 1.1 added ``Extension.event_types`` and
``LogView.complete`` (both trailing fields with defaults). 1.2 added the artifact store's
names a save uses (``ArtifactRef``, ``InputRef``, ``RelationRequest``, ``Excerpt``,
``SELF``) and ``SaveRequest.primary_inputs`` and ``SaveRequest.relations`` (ADR-P04). 1.3
added grounding's names (``SpanDraft``, ``CitePolicy``, ``Unknown``, ``maybe_unknown``) and
the trailing fields ``Ok.spans``, ``Excerpt.span`` and ``ArtifactKind.cites`` (ADR-P13).
1.4 added what a completion check reads (``TaskView``, ``InstanceRef``,
``InstanceStatus``) and the trailing field ``Agent.concurrency`` (ADR-P15). 1.5 added
the model interface's names (the specs, thresholds, requests, results, the rule-based
fake bases and the fixed-point helpers) and the trailing fields ``ModelAlias.fields``,
``ModelAlias.questions`` and ``ModelAlias.gates_effect`` (ADR-P07). 2.0 (26 Nov 2026,
decision 34: compaction is a must-have) changed defaults, a major change:
``ContextPolicy.masking`` is True and ``ContextPolicy.summary`` is ``SummaryPolicy()``,
so an agent that declares a policy to change one number keeps masking and summaries;
``SummaryPolicy.alias`` is the implicit ``generate`` and ``SummaryPolicy.trigger_chars``
is None (the high watermark); ``Agent.requires_api`` defaults to ``">=2.0,<3"``. It also
added the trailing field ``Ok.reads`` (a body's recorded reads, ADR-P04 D6). 2.1 added the
record reader a tool body reads through (``RecordReader``, ``ReturnedText``) and the
trailing field ``ToolContext.reader`` (ADR-P13 D1). 2.2 added what a contributed system
of record implements and answers (``SystemOfRecord``, ``SystemReceipt``; ADR-P20 S2) and
the trailing field ``ToolContext.run_date``, the run's recorded start date. 2.3 (26 Nov 2026,
decision 36) added ``ResumePolicy`` and the trailing field ``ContextPolicy.resume``: the
fallback rules of resume on the existing session, whose setting is per run. 2.4 (26 Nov
2026, tier 3: ADR-P12, P26, P28) added the trailing fields ``ToolContext.http`` (the
policed HTTP client) and ``ToolContext.exec`` (the sandbox's exec API),
``SandboxNeeds.outputs``, ``SandboxNeeds.limits`` and ``EnvironmentSpec.preapprove_pinned``,
and the names ``ExecLimits``, ``TreeManifest``, ``ExecOutcome``, ``HttpResponse`` and
``EgressRefused``.
2.5 added ``where_part`` and ``part_version`` (record parts, ADR-P05 D6 as amended).
2.6 (26 Nov 2026, ADR-P25) added ``observe_time`` and the trailing fields
``ToolContext.time_reads`` (the attempt's time reads; reading ``run_date`` is now a
recorded read), ``ApprovalRule.expires_after_s`` and ``ArtifactKind.max_age_s``.
2.7 (26 Nov 2026, ADR-P29) added ``TransientEffectError``, so a contributed system of
record can ask for a retry, and external record state's ``ExternalRecord`` and
``external_records``.
2.8 (26 Nov 2026, ADR-P09) added what memory needs from the core: the trailing fields
``SaveRequest.supersedes`` (a save that replaces its slot's latest version for every
reader) and ``ToolContext.artifacts`` (a read tool's ``ArtifactReader`` over its
``reads_kinds``, answering ``ArtifactSnapshot``), ``TaskView.called``, the projection
``ended_runs`` and its ``RunEnded``, and the instruction-pattern check
``instruction_text`` with its flag ``INSTRUCTION_FLAG``.
2.9 (27 Nov 2026) added fast-class calls from a tool body (``ToolSpec.model_aliases``,
the trailing field ``ToolContext.models``, ``ToolModels``, ``Judgement``, ``passage_id``,
``span_draft``; ADR-P07 G1), structural reading (``sniff_format``, ``segment``,
``Segment``, ``read_rows``, ``Rows``, ``Row``, ``visible``, ``parse_date``,
``find_dates``, ``FORMATS``; ADR-P13 G3), and reading a record of unknown kind
(``DocumentKind``, ``DocumentReading``, ``OTHER_KIND``, ``decide_kind``,
``document_preview``, ``kind_question``, ``read_document``; the field types ``time``,
``datetime``, ``estimate`` and ``forecast``; ``FieldSpec.adapted``; ``allow_adapted`` on
an extraction alias; ADR-P07 G3), and ``record_sections`` and ``section_heading``, so a
record holding several kinds is decided section by section (ADR-P07 G4).
2.10 (27 Nov 2026, decision 38) added invalidation modes: the trailing fields
``Agent.invalidation`` (``off`` by default) and ``ToolSpec.edits``, and the names
``InvalidationMode`` and ``EditSpec``, and ``RecordReader.serves``.
2.11 (27 Nov 2026) added the web (ADR-P26 A3): the trailing field ``ToolContext.web``,
``web_search_tool``, ``fetch_url_spec``, ``fetch_text``, ``FetchedText``, ``page_read``,
``WEB_SOURCE``, ``WEB_EGRESS`` and ``web_record_id``.
2.12 (27 Nov 2026, ADR-P05 amendment M2) added citation keying: the trailing field
``Agent.invalidation_keys`` (``reads`` by default) and the name ``InvalidationKeys``.
2.13 (27 Nov 2026) added cross-run reuse (ADR-P04 R1): the trailing fields ``Agent.reuse``
(``on`` by default) and ``TaskView.hidden`` (the versions a check does not see).
2.14 (27 Nov 2026, ADR-P10 T1 and T2) added the harness's input and common tools:
``input_tools``, ``InputReading``, ``INPUT_TOOLS``, ``read_view``, ``fetch_url_tool``,
``todo_tool``, ``think_tool`` and ``OfflineReaders``.
2.15 (27 Nov 2026, ADR-P07 G6) added native file input: the trailing fields
``Message.files`` (no files by default; a message with none hashes as before) and
``ModelCapabilities.file_types`` and ``ModelCapabilities.max_inline_bytes`` (none).
2.16 (27 Nov 2026, ADR-P16 S4) added ``Recite``, the recitation stop policy a supervisor
reads its work board through, ``RECITATION`` (the fence line a model reads it under), the
trailing field ``Prep.data`` behind it, and
``TaskView.spent``, the turns and tool calls spent below an instance on one key.
2.17 (27 Nov 2026, ADR-P14 B2) added the trailing fields ``Limits.jitter`` (0 by default,
so a backoff is the same every time) and ``Limits.jitter_source``, the injected source a
jittered backoff draws from; ``Limits.max_retries`` and ``Limits.backoff`` are now the
model step's.
2.18 (28 Nov 2026, ADR-P04 K1) added the trailing field ``ArtifactKind.compute``: the
fields a save computes from the content, written over the model's (counts a check asserts).
2.19 (28 Nov 2026, ADR-P04 K1e) added the trailing field ``ArtifactKind.compute_reads``: the
kinds whose current content a ``compute`` is given too (a need's theme matched to the scope).
2.20 (28 Nov 2026) added the trailing field ``ToolContext.environment``: the environment's own
note on the supplied records (a fixture world's), which the input tools return with every
result. 3.0 (28 Nov 2026, decision 42: long-running tasks) changed defaults, a major change:
``Task.turn_limit`` and ``Limits.turn_limit`` are ``int | None`` and None by default, so a
Task has no turn limit unless it declares one, and the run budget is the backstop.
``Agent.requires_api`` defaults to ``">=3.0,<4"``; an extension that reads no turn limit
can accept both majors (``">=2.0,<4"``).
3.1 (28 Nov 2026, the person chooses) added ``AnswerSave`` and the completion check
``chosen_by_person``, the trailing fields ``DeclaredInterrupt.options``,
``DeclaredInterrupt.multiple`` and ``DeclaredInterrupt.saves`` (a choice whose options code
builds and whose answer the runtime saves), ``SaveRequest.answer`` and
and ``ArtifactVersion.answer``. A kind a declared choice saves is the runtime's alone: a
tool's save of it is refused ``answer_only``.
"""

Version = tuple[int, int]
Clause = tuple[str, Version]

_CLAUSE = re.compile(r"^(>=|<=|==|>|<)\s*(\d+)(?:\.(\d+))?$")
_COMPARE = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def parse_version(text: str) -> Version:
    """``"1.2"`` -> ``(1, 2)``; ``"2"`` -> ``(2, 0)``. Raises ``ValueError`` otherwise."""
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?", text.strip())
    if not match:
        raise ValueError(f"not a major.minor version: {text!r}")
    return int(match.group(1)), int(match.group(2) or 0)


def parse_requirement(spec: str) -> tuple[Clause, ...]:
    """The clauses of ``spec``. Raises ``ValueError`` on an empty or malformed specifier."""
    clauses: list[Clause] = []
    for part in spec.split(","):
        match = _CLAUSE.match(part.strip())
        if not match:
            raise ValueError(f"malformed requires_api clause {part.strip()!r} in {spec!r}")
        op, major, minor = match.groups()
        clauses.append((op, (int(major), int(minor or 0))))
    return tuple(clauses)


def satisfies(spec: str, version: str) -> bool:
    """True when ``version`` meets every clause of ``spec``."""
    have = parse_version(version)
    return all(_COMPARE[op](have, want) for op, want in parse_requirement(spec))
