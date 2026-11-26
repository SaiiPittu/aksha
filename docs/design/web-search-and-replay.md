# Web search, a world's web, and record and replay

*Design note, 27 Nov 2026. Status: built; the ADR amendments it implements are Proposed (ADR-P26 A7 to A11, ADR-P22 R1 to R5).*

## The model: a general agent, a specific task

An agent's definition is the same offline and live: its system prompt, its Task briefs, its tools and its completion checks describe the job in general ("search for who sells what the request is about, fetch the results worth reading, cite only what you fetched"). Everything specific to one piece of work belongs to the task and its world:

| Part of a task | Where it lives |
|---|---|
| What the person asks | the request (an eval case's `opening`) |
| What the person uploads | the world's `sources/` (a user's own folder, live) |
| The web the agent can search and fetch | the world's `web.json` and `web/` (a search API and the real web, live) |
| What counts as right | the world's `key.json` and the suite's graders |
| What the offline model is told about the market | `web.json`'s `fake` hints (nothing, live) |

So one research agent runs the stillroom, tilldew and cindral tasks unchanged, and the same agent runs live once a model and a search API are configured. The architecture guard `tests/architecture/test_agent_generality.py` refuses any fixture value in an agent's logic or prompts.

## Search and fetch are ports

Two runtime ports, in `runtime/web/ports.py`: `WebSearch.search(query, limit)` returns ranked `SearchHit` values (title, URL, snippet, date), and `WebFetch.fetch(url, http)` returns a `FetchedPage`. A tool body never holds a port. It calls `ToolContext.web`, a handle the agent loop builds per attempt (`AttemptWeb`), which:

1. searches through the search port and returns the hits, keeping nothing, so a snippet is never a text the runtime returned and can never be minted as a citation span;
2. fetches through the fetch port with the attempt's HTTP client, the one bound to the fetch tool's egress policy, and keeps the page as a returned text of the source `web`, record ID `web/<host><path>`, version `sha256:` of its text. A span drafted over the page then mints like a span over any input (ADR-P13 D1), and lineage records the page at its version.

Only a tool declared under the `aksha.web` egress policy (both generic tools are) is handed the handle; every other tool's `ctx.web` is None, so no reader or save can reach a search provider.

A live fetch of a PDF or an Office file goes through document ingestion (ADR-P31): the page's text is ingestion's header and representation, so the model reads it the way it reads an uploaded file of that type.

The generic tools are in `runtime/web/tools.py`, exported through `aksha.runtime.api` (API 2.10): `web_search_tool(agent)` and `fetch_url_spec(agent, ...)`. An agent that reads pages its own way writes its own `fetch_url` body over `fetch_text` and keeps the spec: the research agent reads a fetched page exactly as `fetch_page` reads an input (its fast readers decide the kind and extract), and the patent agent reads a fetched publication as `fetch_publication` reads one (a one-record reader, `OneRecord`, under the same `SourceWorld` rules).

A live fetch runs under the runtime's own egress policy `aksha.web`: any public host over HTTPS, never a private, loopback, link-local or metadata address, every redirect checked again, every decision recorded on the step (ADR-P26 D2 to D4). With `AKSHA_NETWORK` off, it is refused `network_off`.

## Adapters, picked by `AKSHA_SEARCH`

| Setting | Search | Fetch |
|---|---|---|
| unset | none: `web_off` | none: `web_off` |
| `world`, `world:<name or folder>` | `WorldWeb`, a world's search proxy | the page's file in the world |
| `brave` | `BraveSearch` (`AKSHA_BRAVE_API_KEY`) | `HttpFetch` over the policed client (needs `AKSHA_NETWORK=on`) |
| `replay:<folder>` (set by `aksha replay`) | the recorded hits | the recorded pages |

A missing key is refused at start (`search_key_missing`), as is a live provider with the network off (`network_off`). Keys are read from the environment and never recorded.

## A world's web, with noise

Each world declares a small web in `web.json`: pages under `.example` domains, each one's file at the world's `web/<host><path>`, the same path as its record ID, so a citation of a fetched page names the world file that holds it. The world's vendor pages (and, in tilldew and cindral, its prior-art publications) moved there from `sources/`, so what a person uploads and what an agent finds on the web are separate, as they are live.

The proxy answers any query with every page that shares a word with it (a word, or one it begins, as a stemming engine matches `salin` to `salinity`), ranked three points for a title word and one for a body word, ties in the declared order, deterministic for a query so a replay is stable. Each world mixes the relevant pages with noise, marked in `web.json` and shown to no tool or model:

| Noise | Stillroom example | What a careful agent does |
|---|---|---|
| `near_duplicate` | a listing site's verbatim copy of a vendor page | skips a result with the title and snippet of one it already chose |
| `outdated` | the vendor's archived 2025 page with an older price | of two results on one host with mostly the same title, keeps the newer |
| `spam` | a deal site, a filing-service advert | skips a result that reads as an advert |
| `irrelevant` | hotel room occupancy rates | may fetch it; finds it sells nothing; does not use it |
| `keyword_only` | a glossary listing the market's words | the same |

The offline models apply those rules (`choose_results` in each agent's fake) and keep only fetched pages that sell something (research) or publications whose text matches the search (patent). Three graders check the result:

1. `web.no_noise_cited`, an invariant run on every episode: no citation names a noise page;
2. `web.noise_unread`: no advert, no copy of a page already fetched and no outdated version was fetched;
3. `web.fetched_from_results`: every fetched page was returned by a search first.

Each has a self-test mutant (`noise-cited`, `spam-read`, `search-skipped`), so each may gate.

## Record and replay

`aksha run --record FOLDER` (or `AKSHA_RECORD` for every process) wraps every bound model adapter and both web ports so each answer is appended to the folder as it is given:

- `models.jsonl`: each generation by its step key (the step ID without the run) and context hash, and each fast-class call a tool body makes by its alias and request hash, in call order. The event log alone cannot replay a run with fast readers: a tool step records a summary of its model calls, not each answer.
- `search.jsonl` and `fetch.jsonl`: each search by query and limit, each fetch by URL, a refusal with its code.
- `recording.json`: the agent, the message, the answers and approvals given, the start time; `inputs/`: the uploads; `outputs.json`; `trace/`: the run's OTLP JSON (ADR-P21).

The recorded run takes place in a store inside the folder, so the person's own store is untouched and the run's IDs start fresh. `aksha replay FOLDER` opens a fresh store, binds every alias to an adapter that answers from `models.jsonl` (run mode `replay`), serves the web from the recorded answers, fixes the clock at the recorded start, uploads the recorded inputs, sends the message, answers as recorded, writes its outputs and its trace, and compares the outputs. A call the recording does not hold is refused, `replay_missing`, and a generation asked something else is `replay_mismatch`: a replay never invents an answer.

A recording made from a fake run ships in `src/aksha/data/recordings/research-occupancy/` and is replayed by `tests/integration/test_record_replay.py`. A live recording is made the same way once a model and a search key are configured, and replays with neither.

## Limits

See [docs/limitations.md](../limitations.md), items 17 and 18: the offline web is a stand-in for a search engine; live search is tested only against a stub; a world's web holds text pages only (a live fetch reads a PDF or Office file through ingestion); a web page that changes reaches no rebuild; a replay needs the same definition, inputs and answers; only answers by question ID and blanket approvals are recorded.
