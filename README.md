# duckhaven-clients

A [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) holding the
DuckHaven client libraries. Each package is a pure HTTP client of DuckHaven's **public**
REST API — none depends on any DuckHaven server internal — and is published to PyPI as its
own project under **Apache-2.0**.

## Members

| Package | PyPI | Purpose |
|---------|------|---------|
| [`duckhaven-sql-connector`](packages/duckhaven-sql-connector) | [published](https://pypi.org/project/duckhaven-sql-connector/) | DB-API 2.0 (PEP 249) client for DuckHaven's SQL session/statement API — the shared transport for the members below. |
| [`dbt-duckhaven`](packages/dbt-duckhaven) | [published](https://pypi.org/project/dbt-duckhaven/) | dbt-duckdb adapter that routes every statement through the connector. |
| [`dlt-duckhaven`](packages/dlt-duckhaven) | [published](https://pypi.org/project/dlt-duckhaven/) | dlt destination that stages Parquet and loads through the connector. |
| [`duckhaven-cli`](packages/duckhaven-cli) | [published](https://pypi.org/project/duckhaven-cli/) | `dh`, the command-line interface — SQL, catalog, dbt lineage, grants and admin over the REST API. |

`dbt-duckhaven`, `dlt-duckhaven` and `duckhaven-cli` depend on `duckhaven-sql-connector` and
pin a minimum version of it. During co-development that dependency resolves to the local
workspace member (`{ workspace = true }`); once installed from PyPI it resolves to the
published release.

## Install

Each package installs from PyPI on its own; the integrations and the CLI pull a compatible
connector in with them:

```sh
pip install duckhaven-sql-connector   # DB-API 2.0 client
pip install dlt-duckhaven             # dlt destination
pip install dbt-duckhaven             # dbt adapter
pip install duckhaven-cli             # dh, the command-line interface
```

See each package's README for connection and configuration details.

## Development

```sh
uv sync            # create the workspace venv and install all members editable
make lint test     # ruff check + pytest
make fmt           # ruff format
```

Requires Python ≥ 3.11 (CI matrix: 3.11–3.14). No mypy gate — Ruff only, but every module
ships complete type hints and a `py.typed` marker.

## Releasing

Each member versions and publishes independently via a tag **prefix**, e.g.
`sql-connector-v1.2.3`. The release workflow builds only the matching package and publishes
it to PyPI via Trusted Publishing (OIDC). See each package's README and
`.github/workflows/release.yml`.
