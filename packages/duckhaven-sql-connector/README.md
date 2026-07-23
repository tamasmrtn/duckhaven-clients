# duckhaven-sql-connector

A [PEP 249 (DB-API 2.0)](https://peps.python.org/pep-0249/) Python client for
[DuckHaven](https://github.com/tamasmrtn/duckhaven-clients)'s SQL **session** API.

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

## Column types

`cursor.description` carries the result's column types in PEP 249's `type_code` field,
spelled the way DuckDB prints a logical type — the same string `DESCRIBE` returns, so it is
self-describing for parameterized and nested types:

```python
cur.execute("SELECT id, amount, created_at FROM sales.orders")
[(d[0], d[1]) for d in cur.description]
# [('id', 'BIGINT'), ('amount', 'DECIMAL(18,4)'), ('created_at', 'TIMESTAMP WITH TIME ZONE')]
cur.column_types  # the same types on their own
```

Both are `None` against a server (or agent) older than this field, so code that reads them
should tolerate that.

> **Values are not re-typed to match.** Results travel as JSON, so a `DECIMAL` or `HUGEINT`
> arrives as a float with its precision **already lost**, a `BLOB` as hex text, an
> `INTERVAL` as an ISO-8601 duration, and temporal types as ISO-8601 strings. The connector
> reports the true type but does not cast the value, because casting could not restore
> precision that was gone before the client saw it — it would only hide the loss.

## Metadata

For relation introspection (as dbt and BI tools need), the cursor exposes metadata methods;
fetch the rows as usual:

```python
cur.tables(catalog="sales", schema_name="public")
for catalog, schema, name, table_type in cur.fetchall():
    ...

cur.columns(catalog="sales", schema_name="public", table_name="orders")
for catalog, schema, table, column, position, data_type, is_nullable in cur.fetchall():
    ...
# also: cur.catalogs(), cur.schemas(catalog=…)
```

Two things are worth knowing about how these work:

- **`columns()` needs an exact `table_name`.** It reports columns with `DESCRIBE`, which
  describes one relation. `information_schema.columns` is not usable: for an attached
  Iceberg table it returns a single placeholder row (`__` / `UNKNOWN`) instead of the real
  columns, and inconsistently so — a table something has already touched in the session
  reports correctly while the rest do not — so it returns wrong data rather than failing.
  Use `tables()` to enumerate, then `columns()` per relation. `data_type` is DuckDB's
  spelling, the same vocabulary a query result reports.
- **`catalogs()`, `schemas()` and `tables()` read DuckHaven's REST browse endpoints**, not
  SQL. Engine-side enumeration is refused on any workspace with a scoped catalog attached,
  since the engine cannot filter those listings by grant; the REST endpoints can, and
  behave identically on open catalogs. They cost one request per catalog in scope, plus one
  per schema for `tables()`, so pass `catalog=` and `schema_name=` when you can.

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

## Server version

`conn.server_version()` reports the server's release and API-contract version:

```python
v = conn.server_version()
if v is None:
    ...  # server predates GET /api/version — assume the oldest supported behaviour
else:
    print(v.version, v.api_version)  # e.g. "1.4.0", 1
```

`version` is the build/release version; `api_version` is an integer bumped only on a
breaking API change. It is a provenance and coarse-compatibility signal, **not** a feature
list — an additive change (a new field, a newly admitted statement) moves neither — so it
is for support and diagnostics rather than for gating behaviour. A server too old to expose
the endpoint returns `None`.

## Compatibility

The exact server endpoints and fields this client depends on are pinned in
[`contract/duckhaven-openapi.subset.json`](contract/duckhaven-openapi.subset.json) and checked
by the contract test. Regenerate it against a running server with
`make refresh-contract HOST=https://duckhaven.internal` to detect API drift early.

## License

Apache-2.0.
