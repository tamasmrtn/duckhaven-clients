# duckhaven-sql-connector

A [PEP 249 (DB-API 2.0)](https://peps.python.org/pep-0249/) Python client for
[DuckHaven](https://github.com/tamasmrtn/duckhaven-clients)'s SQL **session** API — the DuckHaven analog of
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
    # agent="…-uuid-…",       # optional explicit compute (an agent UUID); omit to auto-pick
) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT ? AS n", [1])   # qmark params, rendered safely client-side
        print(cur.description, cur.fetchall())
```

A runnable version is in [`examples/quickstart.py`](examples/quickstart.py).

> **Note:** The DuckHaven SQL session surface is disabled unless the operator sets
> `SQL_SESSIONS_ENABLED=true` on the server. Against a server with it off, opening a
> session raises an `OperationalError`.

## Errors

Failures raise the standard [PEP 249 exceptions](https://peps.python.org/pep-0249/#exceptions),
carrying the server's `code`/`status_code`/`detail`:

- `ProgrammingError` — a rejected statement (`statement_not_allowed`), a denied grant, or a
  missing object.
- `OperationalError` — an unavailable/disconnected agent, a reaped or closed session
  (reconnect), a timeout, or the session surface being disabled. `MaxRetryDurationError`
  (a subtype) is raised when retries exhaust the configured time budget.
- `InterfaceError` — bad connection configuration or a malformed response.

Idempotent requests (poll/fetch/cancel) are retried on transient failures with capped
exponential backoff; a server `Retry-After` header is honored, and retries are bounded by
both a max-attempt count and a total-time budget (`RetryPolicy.max_elapsed`). Statement
submits are never auto-retried.

## Metadata

For relation introspection (as dbt and BI tools need), the cursor exposes metadata methods
that query the server's `information_schema`; fetch the rows as usual:

```python
cur.tables(schema_name="public")
for catalog, schema, name, table_type in cur.fetchall():
    ...
# also: cur.catalogs(), cur.schemas(catalog=…), cur.columns(table_name=…)
```

## Arrow results

With the `arrow` extra, fetch results as a `pyarrow.Table`:

```python
cur.execute("SELECT * FROM sales.orders")
table = cur.fetch_arrow_table()
```

## Observability

- **`otel` extra** — each request emits a client span and injects a W3C `traceparent`, so
  client spans join the DuckHaven server trace. It is a no-op when the extra isn't installed.
- **Hooks** — pass `connect(..., hooks=Hooks(...))` to observe request timings, retries, and
  rows fetched without any OpenTelemetry dependency (a client library runs no metrics server).

## Compatibility

The exact server endpoints and fields this client depends on are pinned in
[`contract/duckhaven-openapi.subset.json`](contract/duckhaven-openapi.subset.json) and checked
by the contract test. Regenerate it against a running server with
`make refresh-contract HOST=https://duckhaven.internal` to detect API drift early.

## License

Apache-2.0.
