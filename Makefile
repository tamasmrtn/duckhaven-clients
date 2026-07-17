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

# Live tests against a real DuckHaven; needs DUCKHAVEN_TEST_HOST/WORKSPACE/PAT.
test-integration:
	uv run pytest packages/duckhaven-sql-connector/tests/integration -m integration

# dbt-duckhaven conformance (dbt-tests-adapter) + e2e against a live DuckHaven; needs
# DUCKHAVEN_TEST_HOST/WORKSPACE/PAT/CATALOG.
test-dbt-integration:
	uv run pytest packages/dbt-duckhaven/tests/functional packages/dbt-duckhaven/tests/e2e \
		-p dbt.tests.fixtures.project -m integration

# dlt-duckhaven e2e: a real dlt pipeline (append + merge) against a live DuckHaven; needs
# DUCKHAVEN_TEST_HOST/WORKSPACE/PAT/CATALOG.
test-dlt-integration:
	uv run pytest packages/dlt-duckhaven/tests/e2e -m integration

# Refresh the pinned OpenAPI contract from a running server: make refresh-contract HOST=https://...
refresh-contract:
	uv run python packages/duckhaven-sql-connector/scripts/refresh_contract.py $(HOST)

# Build a single member: make build PKG=duckhaven-sql-connector
build:
	uv build --package $(PKG)

# Validate built wheel/sdist metadata before publishing.
check-dist:
	uv run twine check dist/*

clean:
	rm -rf dist build .pytest_cache .ruff_cache .coverage
	find . -name '_version.py' -delete
