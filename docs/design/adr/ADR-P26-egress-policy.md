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

### Amendments from the build (26 Nov 2026, Proposed)

Built on 26 Nov 2026: the policy check, the runtime's HTTP client and its adapters, the records, and the source scan.

1. A1, deny by default. An empty `allow_hosts` reaches no host (`host_not_allowed`), where the interface sketch read it as any public host; `"*"` names any host and still refuses private, loopback, link-local and metadata addresses. Entries are exact hosts, `*.` subdomains (not the bare domain), `*`, or CIDR ranges; an allow range matches a host whose every resolved address lies in it, and a deny range refuses a host any of whose resolved addresses lies in it (`host_denied`).
2. A2, the network is off per process unless asked. The composition root builds the resolver and transport only with `AKSHA_NETWORK=on`; otherwise every tool's client refuses `network_off`, so a default run stays offline. The setting is recorded on `run.started` under `sandbox.network`.
3. A3, D1 becomes a runtime default, D2 a load-time scan. The loader cannot tell which body reads the network, so `egress_policy_missing` is not a load refusal: a tool with no `egress` gets a client that refuses every URL (`no_egress_policy`), and a policy no loaded extension declares refuses `egress_policy_unknown`. The source scan refuses `network_import` for `socket`, `ssl`, `urllib.request`, `http.client`, `http.server`, `requests`, `httpx`, `aiohttp`, `urllib3` and the mail and file-transfer clients in agent and extension modules (`urllib.parse` and `http.HTTPStatus` stay allowed). A hidden import is beyond the scan, which is why only trusted folders load.
4. A4, the record and the refusal (D4). Every hop's decision (policy, URL, host, resolved addresses, allowed, reason) goes on the step's `step.completed` under `value["egress"]`, refused or not. A refusal raised out of a body becomes `Err("egress_refused", item=<host>)`; the loop removes `egress_refused` from every tool's `retry_on`, so it is never retried even when a tool names it.
5. A5, the ports. `HttpClient` (`ToolContext.http`), `Resolver` and `Transport` are in `runtime/egress/ports.py`; `check_url` and `check_destination` are pure; `PolicedHttpClient` resolves, checks every address, records, and connects to the first checked address, so a name is never re-resolved between the check and the connect; TLS still names and verifies the URL's host. Redirects (at most five) are new hops; a URL with credentials is refused; bodies are capped at 5 MiB. The adapters are `adapters/egress/network.py`.
6. A6, environments use the same client. ADR-P28's fetch step runs the policed client under the environment spec's egress policy; its decisions are in the build record.

### Amendments from the web tools (27 Nov 2026, Proposed)

Built on 27 Nov 2026; the design note is [web search and replay](../web-search-and-replay.md).

7. A7, search and fetch are runtime ports. `WebSearch.search(query, limit)` and `WebFetch.fetch(url, http)` are in `runtime/web/ports.py`, held by `ExecutionTier.search` and `ExecutionTier.web`. A body reaches them only through `ToolContext.web` (a trailing field, extension API 2.10), the per-attempt `AttemptWeb`; None, or no port, answers `web_off`.
8. A8, a fetched page is a returned record, a snippet never is. `AttemptWeb.fetch` keeps the page through `AttemptReader.keep` as a text of the source `web`, record ID `web/<host><path>` (query kept, fragment dropped, the root `/index`), version `sha256:` of the text, so its passages mint as spans (ADR-P13 D1) and lineage records the page at its version. A search keeps nothing, so a span over a snippet is refused at mint. A relation may join a page to an input (`record:web`).
9. A9, the web fetch's policy is the runtime's own. `aksha.web` (`runtime/web/types.py`) allows any public host over HTTPS and refuses private, loopback, link-local and metadata addresses; any tool may name it without an extension (`BUILTIN_EGRESS`, added to every agent's egress rights). `fetch_url` declares it, so a live fetch is checked hop by hop and recorded like any other egress, and is refused `network_off` unless `AKSHA_NETWORK=on`.
10. A10, the adapters. `adapters/search/world.py` (a world's offline web: a ranked proxy with noise, and the page's file as the fetch), `adapters/search/brave.py` (a live search API, its key from `AKSHA_BRAVE_API_KEY`, its 429 and 5xx answers transient) and `adapters/web/http.py` (plain HTTP through the policed client; a binary answer refused `unsupported_content`), picked by `AKSHA_SEARCH`. A live provider without its key is refused `search_key_missing` at start, and with the network off `network_off`. The search adapter calls its one provider host directly, outside a tool's policy; every page a result names is fetched under `aksha.web`.
11. A11, the generic tools. `web_search_tool(agent)` (a read that mints nothing and reaches no network) and `fetch_url_spec(agent)` (a read under `aksha.web` that mints spans), with `fetch_text` and `page_read` for an agent's own `fetch_url` body, are exported by `aksha.runtime.api`. The research and patent agents list both; each reads a fetched page as it reads an input of the same kind.

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
