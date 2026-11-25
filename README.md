# Aksha

Aksha is a small, offline, pluggable agent harness in Python. It runs agents defined as data, gates every tool call in code, pauses for approval or missing information, and resumes after a restart without repeating a write or a send.

It ships with two agents: a Research Agent and a Patent Agent.

## Status

Design in progress. The design documents (problem, users, requirements, high-level and low-level design, decision records) come first; the code follows them.

## Development

Aksha needs Python 3.12 or newer. The runtime package depends on the standard library and `jsonschema` only; `pytest`, `ruff` and `mypy` are development tools.

The layout is the src layout from the Python Packaging User Guide: the package is `src/aksha/` (the runtime core is `src/aksha/runtime/`), so the tests import the installed package (`make venv` installs it editable), never the working tree by accident, and tests are in `tests/`. `web/` and `scratch-pad/` are not part of the Python package.

```sh
make venv        # create .venv with uv and install aksha (editable) with dev tools
make venv-postgres  # also install the PostgreSQL driver, so the PostgreSQL tests run
make test        # pytest (the PostgreSQL tests skip, saying why, if they cannot run)
make test-postgres  # the storage tests, failing instead of skipping without PostgreSQL
make lint        # ruff check and ruff format --check
make typecheck   # mypy --strict over src/aksha/ and tests/
make check       # all three
```

Code layout (ports and adapters; the full tree, the dependency rule and how to add an adapter are in `docs/architecture/codebase-structure.md`):

1. `src/aksha/runtime/` is the domain: one package per primitive, named for the ADR's term (`events`, `artifacts`, `stop_policy`), with the ADR ID in its docstring. Each holds `types.py` (its values), `ports.py` (its Protocols, when it has any) and one module per piece of behaviour, and only code that exists; the structure document lists what is planned.
2. Every `typing.Protocol` a subsystem depends on is in that subsystem's `ports.py` (for example `src/aksha/runtime/storage/ports.py`).
3. Backends are adapters in `src/aksha/adapters/` (`storage/postgres.py`, `storage/sqlite.py`). The runtime never imports them and needs only the standard library and `jsonschema`; PostgreSQL is the optional extra `aksha[postgres]`.
4. Agents (`src/aksha/agents/`) and extensions (`src/aksha/extensions/`) import only `aksha.runtime.api`. An agent is one folder exporting one `Agent` value, an extension one package exporting one `Extension` value; the loader (`src/aksha/runtime/definitions/`) checks both before any run and hashes the agent definition. The steps are in the structure document, "How to add an agent or an extension". The CLI and HTTP API will be in `src/aksha/interfaces/`; `src/aksha/app.py` wires adapters in.
5. Tests: `tests/unit/` mirrors the package, `tests/contract/` runs each port's suite against every adapter, `tests/integration/` runs primitives together, and `tests/architecture/` enforces the dependency rule.

The event store has two adapters behind one port and one contract suite: PostgreSQL (the default and the production backend; set `AKSHA_POSTGRES_DSN`) and SQLite (zero setup, for local use, tests and offline runs; `AKSHA_STORE=sqlite`). The PostgreSQL tests start a throwaway docker container named `aksha-pg-test` from a local `postgres:16` image (never pulled) or use `AKSHA_TEST_POSTGRES_DSN`. `make test` skips them, saying why, without psycopg, docker or the image; `make test-postgres` fails instead.

The event store is hash-chained (ADR-P01 Amendment H). Each event row stores `prev_hash`, the hash of the row before it, and `hash`, a SHA-256 over the row's canonical encoding (a subset of RFC 8785: sorted keys, no whitespace, no floats) covering its whole envelope and `prev_hash`. Seq 1 links to a genesis hash computed from the store's random id, so rows copied from another store do not verify. The writer computes the hashes inside the append transaction, under its single-writer lock, and both adapters produce the same hashes for the same events (a contract test checks it). `store.verify()` walks the chain and returns the first broken seq, or `None`; `aksha.runtime.storage.anchors.anchor_head` copies the head `(seq, hash)` to an anchor sink (`src/aksha/adapters/anchor/file.py` is the file one), and `verify_against(store, sink)` also checks every anchor.

Why a hash chain. It is the one idea a blockchain shares with git: a git commit's hash covers its parent's hash, so changing an old commit changes every hash after it. Here event 41's hash covers event 40's, so an edited, reordered or deleted past row breaks the chain at that row. There are no peers, no consensus and no mining; one writer appends. The same harness has to serve one person on a laptop and an unattended software factory, where runs send emails, open pull requests and record approvals with nobody watching, and someone who was not there reads the record later. That reader needs to check the record, not trust it, and the chain makes the check one call that costs one SHA-256 per append. It is tamper-evident, not tamper-proof: anyone who can open the database can drop the triggers, rewrite rows and recompute every later hash, and a truncated tail is still a valid, shorter chain. An anchor held where the writer cannot rewrite it (another machine, a write-once bucket) turns both into detected changes, and WORM (write once, read many) storage for the store or its anchors is what makes the record write-once in fact. On a laptop with no anchor, the chain still catches accidental damage, hand edits and rows copied between stores, but not a determined local editor.

Without `make`, the same commands are `.venv/bin/pytest`, `.venv/bin/ruff check .` and `.venv/bin/mypy`. Tests run with no network.
