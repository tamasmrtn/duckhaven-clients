# duckhaven-clients

A [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) holding the
DuckHaven client libraries. Each package is a pure HTTP client of DuckHaven's **public**
REST API — none depends on any DuckHaven server internal — and is published to PyPI as its
own project under **Apache-2.0**.

## Members

| Package | Status | Purpose |
|---------|--------|---------|
| [`duckhaven-sql-connector`](packages/duckhaven-sql-connector) | in progress | DB-API 2.0 (PEP 249) client for DuckHaven's SQL session/statement API — the shared transport for the two below. |
| `dbt-duckhaven` | planned | dbt-duckdb environment that routes through the connector. |
| [`dlt-duckhaven`](packages/dlt-duckhaven) | in progress | dlt destination that stages Parquet + loads through the connector. |

`dbt-duckhaven` and `dlt-duckhaven` will depend on `duckhaven-sql-connector` as a
`{ workspace = true }` member, so they build against the local connector during
co-development.

## Development

```sh
uv sync            # create the workspace venv and install all members editable
make lint test     # ruff check + pytest
make fmt           # ruff format
```

Requires Python ≥ 3.10 (CI matrix: 3.10–3.14). No mypy gate — Ruff only, but every module
ships complete type hints and a `py.typed` marker.

## Releasing

Each member versions and publishes independently via a tag **prefix**, e.g.
`sql-connector-v1.2.3`. The release workflow builds only the matching package and publishes
it to PyPI via Trusted Publishing (OIDC). See each package's README and
`.github/workflows/release.yml`.
