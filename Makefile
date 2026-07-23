.PHONY: setup lock lint typecheck format check test

## Install project with exact pinned versions from uv.lock
setup:
	uv sync --frozen

## Regenerate the dependency lock file (uv.lock)
lock:
	uv lock

## Run ruff linter on opc/
lint:
	ruff check opc/

## Run type checking with mypy on opc/
typecheck:
	mypy opc/ --ignore-missing-imports

## Auto-format code with ruff
format:
	ruff format opc/
	ruff check --fix opc/

## Run all checks (lint + typecheck)
check: lint typecheck

## Run test suite
test:
	python -m pytest tests/ -q
