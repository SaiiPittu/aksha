PY ?= 3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: venv venv-postgres test test-postgres lint format typecheck check demo

## Create .venv and install the package with dev tools, the server and the trace exporter.
venv:
	uv venv --allow-existing --python $(PY) $(VENV)
	uv pip install --python $(BIN)/python -e ".[dev,server,otel]"

## Add the PostgreSQL driver (extra aksha[postgres]) so the PostgreSQL tests run, not skip.
venv-postgres: venv
	uv pip install --python $(BIN)/python -e ".[dev,server,otel,postgres]"

test:
	$(BIN)/pytest

## The PostgreSQL adapter's tests, failing (not skipping) when they cannot run. Needs psycopg
## (make venv-postgres) and either AKSHA_TEST_POSTGRES_DSN or docker with a local postgres
## image (never pulled; AKSHA_PG_IMAGE picks it, default postgres:16).
test-postgres:
	AKSHA_REQUIRE_POSTGRES=1 $(BIN)/pytest tests/contract tests/unit/adapters/storage tests/unit/test_app.py

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
## On a fresh clone this creates the virtual environment first, so it is one command.
demo: $(BIN)/python
	$(BIN)/python -m aksha.interfaces.demo

$(BIN)/python:
	$(MAKE) venv
