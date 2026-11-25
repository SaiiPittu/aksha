# ADR-P26: Egress policy for tools that reach the network outside the sandbox (lean: declared policy data, enforced by the HTTP client the runtime hands to tool bodies, checked after DNS resolution and on every redirect)

*Intermediate design as of 26 Nov 2026. Status: Proposed. This is a design record, not a description of shipped code. The Research agent and the Patent agent named below are example agents for Stillroom, a fictional hardware venture; the coding agent is a contrast case used only to test the design, and it is not built. "First milestone" means the 27 Nov 2026 cut of the build. Decision numbers refer to the [decisions log](../04-decisions-log.md); PR-NN and PNN IDs name primitives and P-1 to P-4 name design patterns, all listed in the [catalogue](../03-primitives.md); "v2" is the previous, unpublished design revision.*

## Status

Proposed, 25 Nov 2026. New primitive, found missing by the Research stress test (the Research stress review, row 15, missing primitive M4). ADR-P12 turns the network off for programs inside the box, but read and external tools run in-process with full network, and the gate's closed matcher set has no host matcher (ADR-P11 S1). No decisions-log entry covers it. Nothing here was built as of 25 Nov 2026.

## Terms

The terms:

- An **egress policy** is declared data that says which network destinations a tool body may reach: allowed schemes, a host allowlist or denylist, and a refusal of private, loopback and link-local addresses.
- **Server-side request forgery** (SSRF, general usage) is the attack in which content the model read, such as a link inside a fetched page, makes a server-side tool request an internal address.

## Context: what breaks without it

In live mode, a search hit or a link inside a fetched page can point `fetch_page` at `http://localhost:8888/` or a cloud metadata address. The model chose the URL from tool output, so it is model-writable content, and nothing between the model and the socket checks the destination. The product requirements' fixtures are local files, so the first milestone is not exposed; any live adapter is.

## Decision

1. D1, policy is data on the tool. A tool whose body makes network requests names an egress policy (`ToolSpec.egress`, ADR-P10 S3); extensions contribute policies (`Extension.egress`, ADR-P20 S2). The loader refuses a network-reading tool with no policy (`egress_policy_missing`).
2. D2, the runtime owns the client. Tool bodies get an HTTP client through `ToolContext.http`, bound to the tool's policy; a body that opens its own socket is outside the contract and the static check flags `urllib`, `socket` and `requests` imports in tool modules.
3. D3, check the address, not only the URL. The client resolves the host, refuses any resolved address in private, loopback, link-local or metadata ranges unless the policy names it, connects to the checked address, and re-checks on every redirect.
4. D4, a refusal is a tool error the model reads (`egress_refused{host}`), recorded on `step.completed`; it is never retried.

## Alternatives considered

| Option | For | Against | Verdict |
|---|---|---|---|
| A. A URL matcher in the gate (ADR-P11) | One place for permissions | Checks before redirects and DNS, which is where SSRF succeeds | Rejected |
| B. Run every network tool in the sandbox | One wall | Read tools are in-process for speed and determinism | Later option for untrusted tools |
| C. Declared policy enforced in the runtime's client (lean) | Checks the real destination; data the loader can inspect | Tool bodies must use the given client | Chosen |

## Interface

```python
# aksha/runtime/egress.py (proposal; design only)
@dataclass(frozen=True)
class EgressPolicy:
    name: str
    schemes: frozenset[str] = frozenset({"https"})
    allow_hosts: tuple[str, ...] = ()             # empty: any public host
    deny_hosts: tuple[str, ...] = ()
    allow_private: bool = False                   # private, loopback, link-local, metadata ranges

class HttpClient(Protocol):
    def get(self, url: str, *, timeout_s: float) -> "HttpResponse": ...   # raises EgressRefused

def check_destination(policy: EgressPolicy, url: str, resolved: Sequence[str]) -> str | None: ...  # pure
```

## Stress test (25 Nov 2026)

| Agent | Verdict | Scenario |
|---|---|---|
| Research | Fits | Row 15's localhost link is refused at D3 |
| Patent | Fits | Live publication fetch under an allowlist |
| Coding | Fits | Git host allowlist |

## Sources (public)

1. Server-side request forgery as an attack class (general usage, not in our notes).
