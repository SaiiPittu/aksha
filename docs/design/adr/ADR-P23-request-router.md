# ADR-P23: Request router (lean: a runtime classification alias over the loaded agents' purpose text, recorded as a durable step, with a session-scoped `route` interrupt when the classification abstains)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. New primitive, found missing by the Research stress test (the Research stress review, row 27 and missing primitive M1). The catalogue had folded it into "a classification call through PR-07 plus a session API command in PR-19" (catalogue, "Where this stops" item 3), but no ADR designed it: `route.decided` appeared only in ADR-P19's event list, no model alias or fake belonged to anyone, and interrupts were scoped to an instance or a run while the routing question comes before any run exists. No decisions-log entry covers routing. Nothing here was built as of 25 Nov 2026.

## Terms

The product requirements ask that users "choose an agent or describe their request and let the harness route it", that the interface "show which agent handles the request", and that it "ask for clarification when the request is unclear". The terms this ADR defines:

- A **request router** picks the agent that handles a free-text request in a session that names no agent.
- A **route decision** (`route.decided`) is the recorded fact of that choice: The agent, the classification answer behind it, and whether a person chose.
- A **route interrupt** is an interrupt of kind `route`, scoped to the session rather than to a run, that asks the person which agent they meant.

## Context: what breaks without it

| Without it | What happens | Requirement |
|---|---|---|
| Routing as a prompt rule inside one agent | The first agent's model decides whether to hand over, so the choice is unrecorded and a restart can route the same message elsewhere | a stated requirement |
| A router that always picks something | An ambiguous request ("help me with the sensor thing") runs an agent the person did not want, and nothing asks | a stated requirement |
| A router in the interface code | The CLI and the web chat route differently, which is the "every interface becomes its own runtime" failure ADR-P19 exists to prevent | a stated requirement |

## Decision

1. D1, routing is a classification. The router is one classification call (ADR-P07) under the runtime-owned alias `runtime.route`. Its question's options are the loaded agents' names, and each option's text is the agent's `purpose` field (ADR-P20 S1). The state the classifier sees is the message text only, rendered by code; no earlier transcript enters it.
2. D2, the call is a durable step. The classification runs under a deterministic step ID in the session's routing namespace (`session/<id>/route/<message seq>`), the session-scoped form of ADR-P02 D2, so a restart after the decision returns the recorded answer and never re-routes. Recovery settles an open route step in its own place in the fixed order (ADR-P02 D5): a completed step with no decision writes the decision or the interrupt from the recorded answer, and a started step with no completion re-runs the classification, which is `at_least_once`.
3. D3, abstention asks; it never falls back to generation. The abstention band of ADR-P07 is the trigger: an answer below `accept_at`, or with a margin under `margin` between the top two agents, raises a `route` interrupt. The interrupt lists the two or three highest-scoring agents with their purpose lines and an "other" choice. It never asks a generation model to guess.
4. D4, the route interrupt is session-scoped. `route` joins `InterruptKind` (ADR-P17 D2, S5) with a `SessionRef` scope. It holds the session's intake, not any run, so the hold predicate (ADR-P16 D6) treats it as human state on its `SessionRef` hold scope: no run opens for that message until the person answers. The person answers with `AnswerInterrupt(interrupt_id, agent=...)` (ADR-P19), which writes `interrupt.resolved` and `route.decided{by: person}` in the transaction that opens the run.
5. D5, a named agent skips the router. `SendMessage(agent="patent")` records `route.decided{by: person}` with no classification step. The CLI's `--agent` flag and the web chat's agent picker both use it.
6. D6, the route decision is the label. `route.decided{agent, by: classifier | person, step_id}` is written in the transaction that opens the run, and the transcript projection labels the reply with that agent.
7. D7, a message to a session with an open run is not routed. ADR-P19 S2 refuses it `lane_required`; routing applies to new requests only.

Worked example, toy numbers (shown as fractions; recorded as parts per million, so 0.91 is stored as 910000). Two agents are loaded. "Who else does camera-free occupancy sensing?" classifies Research 0.91, Patent 0.05; `accept_at` is 0.8, so the run opens labelled Research. "Help me with the sensor thing" classifies Research 0.48, Patent 0.44; the margin 0.04 is under 0.2, so a `route` interrupt asks "Research competitors and customers, or prepare invention disclosures?". The answer records `route.decided{by: person}` and opens the run.

## Alternatives considered

| Option | What it is | For | Against | Verdict |
|---|---|---|---|---|
| A. A router session that judges and delegates | A long-running model session routes and tracks work | Flexible; can split one request | A generation model decides, so the choice is not thresholdable; a second store of tickets beside the event store | Rejected for v1; Aksha's supervisor Task (ADR-P15) covers multi-agent work |
| B. Keyword rules in the interface | The CLI matches words to agents | Cheap | Each interface routes differently; nothing asks when unclear | Rejected |
| C. Handoff tools inside each agent | Each agent's model may call `handoff_to(agent)` | No new primitive | The first agent always runs first; the decision lives in a model turn | Rejected |
| D. A classification alias with an abstention band and a session-scoped interrupt (lean) | D1 to D7 | Recorded, thresholdable, testable with a fake; asks exactly when uncertain | One more interrupt scope | Chosen |

## Interface

```python
# aksha/runtime/route.py (proposal; design only)
ROUTE_ALIAS = "runtime.route"                    # the one classification alias the core owns

@dataclass(frozen=True)
class RouteDecision:
    agent: str
    by: Literal["classifier", "person"]
    step_id: str | None                          # the classification step, None when a person named it
    probabilities_ppm: tuple[tuple[str, int], ...]   # parts per million, as ADR-P07 D4 stores them

def route_question(agents: Sequence["LoadedDefinition"],
                   thresholds: "ConfidenceThresholds") -> "QuestionSpec": ...   # pure
def route(view: "SessionView", message_seq: int, model: "ClassificationModel",
          agents: Sequence["LoadedDefinition"]) -> RouteDecision | "RouteInterrupt": ...
    # runs the classification as a durable step; returns a decision or raises a route interrupt

# Events: route.decided{agent, by, step_id}; interrupt.raised{kind: "route", scope: SessionRef}
# The answer: AnswerInterrupt(interrupt_id, agent=...) (ADR-P19); the hold: hold_reason(view, SessionRef) (ADR-P16)
```

## Stress test (25 Nov 2026)

This primitive came out of the Research review. Verdicts against the design above, by my reading:

| Agent | Verdict | Scenario |
|---|---|---|
| Research | Fits | The two requests in the worked example |
| Patent | Fits | A disclosure request routes by purpose; a named `--agent patent` skips the router |
| Coding | Fits | Several agents in one session; the router needs nothing coding-specific |
