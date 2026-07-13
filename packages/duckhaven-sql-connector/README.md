# duckhaven-sql-connector

A [PEP 249 (DB-API 2.0)](https://peps.python.org/pep-0249/) Python client for
[DuckHaven](https://github.com/duckhaven)'s SQL **session** API — the DuckHaven analog of
`databricks-sql-connector`.

It is a **pure HTTP client of DuckHaven's public REST API**: it authenticates with a
DuckHaven Personal Access Token (`dh_pat_…`), opens a SQL session bound to one compute
agent, runs statements against that session's persistent DuckDB connection, and fetches
results. It never talks to a compute node directly and depends on no DuckHaven server
internals.

This connector is the shared transport that `dbt-duckhaven`, the dlt `duckhaven`
destination, a future CLI, and Airflow operators build on.

## Install

```sh
pip install duckhaven-sql-connector
# optional extras:
pip install "duckhaven-sql-connector[arrow]"   # client-side Arrow tables
pip install "duckhaven-sql-connector[otel]"    # OpenTelemetry trace propagation
```

## Usage

```python
from duckhaven_sql_connector import connect

with connect(
    host="https://duckhaven.internal",
    workspace="analytics",
    token="dh_pat_…",
    catalog="sales",          # optional default catalog
    agent="warehouse-a",      # optional explicit compute; omit to let the API pick
) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        print(cur.fetchall())
```

> **Note:** The DuckHaven SQL session surface is disabled unless the operator sets
> `SQL_SESSIONS_ENABLED=true` on the server. Against a server with it off, opening a
> session raises an `OperationalError`.

## License

Apache-2.0.
