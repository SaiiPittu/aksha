PY ?= 3.12
VENV := .venv
BIN := $(VENV)/bin

# In a checkout, local state (the store, blobs, demo and eval output) stays in var/, which
# git ignores. An installed aksha uses the per-user data folder instead (aksha doctor).
export AKSHA_HOME ?= var

.PHONY: venv venv-postgres test test-postgres lint format typecheck check demo dist release-check

## Create .venv and install the package with the dev group, the server, the trace exporter
## and the ingest extension's parsers (so their tests run rather than skip).
venv:
	uv venv --allow-existing --python $(PY) $(VENV)
	uv pip install --python $(BIN)/python -e ".[server,otel,ingest]" --group dev

## Add the PostgreSQL driver (extra aksha-harness[postgres]) so the PostgreSQL tests run.
venv-postgres: venv
	uv pip install --python $(BIN)/python -e ".[server,otel,ingest,postgres]" --group dev

test:
	$(BIN)/pytest

## The PostgreSQL adapter's tests, failing (not skipping) when they cannot run. Needs psycopg
## (make venv-postgres) and either AKSHA_TEST_POSTGRES_DSN or docker with a local postgres
## image (never pulled; AKSHA_PG_IMAGE picks it, default postgres:16). The second run is the
## PostgreSQL variant of every integration test parameterised over the backend (budgets,
## cancellation, durable execution, effect steps, the outbox): the SQLite job skips them for
## want of a driver, so this is the only job that runs them. A module that adds a postgres
## parameter is picked up by its id; an empty selection fails (pytest exit code 5).
test-postgres:
	AKSHA_REQUIRE_POSTGRES=1 $(BIN)/pytest tests/contract tests/unit/adapters/storage tests/unit/test_app.py
	AKSHA_REQUIRE_POSTGRES=1 $(BIN)/pytest tests/integration -k postgres

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

typecheck:
	$(BIN)/mypy

check: lint typecheck test

## The change-and-recovery demo: offline, fake models, no keys, about 20 seconds. Writes var/demo/.
## On a fresh clone this creates the virtual environment first, so it is one command. It is
## `aksha demo`, the same command an installed package offers.
demo: $(BIN)/python
	$(BIN)/aksha demo --work var/demo

## Build the sdist and the wheel into dist/ and check their metadata (twine check --strict).
dist:
	rm -rf dist
	uv build --out-dir dist
	uvx --from 'twine>=6.1' twine check --strict dist/*

## Everything the release workflow checks before an upload, run locally: build, check, then
## install the wheel into a fresh virtual environment outside the repo and run aksha there.
release-check: dist $(BIN)/python
	AKSHA_DIST=dist $(BIN)/pytest -p no:cacheprovider tests/package

$(BIN)/python:
	$(MAKE) venv
