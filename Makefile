.PHONY: sync lint fmt fmt-check test test-cov build check-dist clean

sync:
	uv sync

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

test:
	uv run pytest

test-cov:
	uv run pytest --cov=duckhaven_sql_connector --cov-report=term-missing --cov-fail-under=90

# Build a single member: make build PKG=duckhaven-sql-connector
build:
	uv build --package $(PKG)

# Validate built wheel/sdist metadata before publishing.
check-dist:
	uv run twine check dist/*

clean:
	rm -rf dist build .pytest_cache .ruff_cache .coverage
	find . -name '_version.py' -delete
