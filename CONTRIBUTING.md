# Contributing

Changes are reviewed as [REVIEWING.md](REVIEWING.md) describes. Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are needed. The `make` targets keep local state in `var/` (`AKSHA_HOME=var`).

## Commands

```sh
make venv           # .venv with aksha (editable), dev tools, the server and the trace exporter
make venv-postgres  # also install the PostgreSQL driver, so the PostgreSQL tests run
make test           # pytest; the PostgreSQL tests skip, saying why, if they cannot run
make test-postgres  # the storage tests, failing instead of skipping without PostgreSQL
make lint           # ruff check and ruff format --check
make typecheck      # mypy --strict over src/aksha/ and tests/
make check          # all three
make demo           # the change-and-recovery demo
```

Tests are in `tests/`: `unit/` mirrors the package, `contract/` runs each port's suite against every adapter, `integration/` runs primitives together (including a real SIGKILL recovery harness and the CLI and HTTP server), `agents/` runs the example agents on the fixture worlds, and `architecture/` enforces the dependency rule. The PostgreSQL tests use `AKSHA_TEST_POSTGRES_DSN`, or start a throwaway container from a local `postgres:16` image that is never pulled.


## Releasing

`make dist` builds and checks the distributions and `make release-check` installs the wheel in a fresh environment and runs it; publishing is the maintainer's, by a version tag, as [docs/releasing.md](docs/releasing.md) describes.

## Further reading

1. [docs/design/README.md](docs/design/README.md): the reading order, from problem and goals to the ADRs.
2. [docs/design/04-decisions-log.md](docs/design/04-decisions-log.md): the maintainer's numbered decisions.
3. [REVIEWING.md](REVIEWING.md): how changes to this repository are reviewed.
