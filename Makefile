PY ?= 3.12
VENV := .venv
BIN := $(VENV)/bin

.PHONY: venv venv-postgres test test-postgres lint format typecheck check

## Create .venv and install the package with dev tools (editable).
venv:
	uv venv --allow-existing --python $(PY) $(VENV)
	uv pip install --python $(BIN)/python -e ".[dev]"

## Add the PostgreSQL driver (extra aksha[postgres]) so the PostgreSQL tests run, not skip.
venv-postgres: venv
	uv pip install --python $(BIN)/python -e ".[dev,postgres]"

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
