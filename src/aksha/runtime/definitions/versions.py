"""The extension API version, and ``requires_api`` specifiers such as ``">=2.0,<3"`` (D2).

``API_VERSION`` is ``major.minor``: adding a public name bumps the minor version; removing
or changing one bumps the major. :mod:`aksha.runtime.api` re-exports it.

A specifier is comma-separated clauses, each an operator (``>=``, ``>``, ``<=``, ``<``,
``==``) and a ``major[.minor]`` version. A missing minor reads as 0.
"""

from __future__ import annotations

import re

API_VERSION = "2.7"
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
