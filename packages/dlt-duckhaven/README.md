# dlt-duckhaven

A [dlt](https://dlthub.com) **destination** that loads data into
[DuckHaven](https://github.com/tamasmrtn/duckhaven-clients)-governed Iceberg tables through
the DuckHaven API.

It is a **staged-Parquet SQL destination** built on the `duckhaven-sql-connector`, modeled
on dlt's `databricks`/`athena` destinations: dlt writes Parquet, the destination stages it
to the workspace object storage, then issues the load command (`COPY`/`INSERT … SELECT
read_parquet(...)`) through the DuckHaven **session API**, which dispatches to an agent that
writes the Iceberg table. **Control goes through the API; bulk data goes through storage
staging** — the same split Databricks and MotherDuck use.

> **Status: alpha.** The destination, its capabilities/type mapping, staged loads
> (`append`/`replace`/`merge`), and schema evolution are implemented and tested. Loading
> uses the session's presigned-URL stage
> (`POST …/sql/sessions/{id}/staging-files`); a DuckHaven with that endpoint and
> `SQL_SESSIONS_ENABLED=true` is required at runtime. See `CHANGELOG.md`.

## Install

```sh
pip install dlt-duckhaven
# pip install "dlt-duckhaven[otel]"  # optional per-load-job spans
```

No storage SDK (s3fs/adlfs) is needed — staged files are uploaded to a presigned URL over
plain HTTPS.

## Configure

`destination="duckhaven"` reads the following (via `dlt.yml`, `.dlt/secrets.toml`, or env):

```toml
[destination.duckhaven]
host = "https://duckhaven.internal"
workspace = "analytics"
agent = "…-uuid-…"        # optional; omit to let the API pick compute
catalog = "raw"

[destination.duckhaven.credentials]
token = "dh_pat_…"        # a DuckHaven Personal Access Token (service account)
```

```python
import dlt

pipeline = dlt.pipeline(
    pipeline_name="ingest",
    destination="duckhaven",
    dataset_name="analytics",   # the Iceberg schema/namespace within `catalog`
)
pipeline.run(my_resource)
```

`dataset_name` is the Iceberg schema; `catalog.dataset_name.table` is the fully-qualified
relation. Auth is a DuckHaven service-account PAT — every statement is authorized and
audited at the API, so a dlt load is fully governed.

### Config reference

| Field | Required | Description |
|-------|----------|-------------|
| `host` | yes | DuckHaven API base URL (`https://…`). |
| `workspace` | yes | Workspace slug the session opens in. |
| `credentials.token` | yes | A DuckHaven service-account PAT (`dh_pat_…`). May also be passed as the `credentials` value directly. |
| `catalog` | recommended | DuckHaven (Polaris) catalog that qualifies loaded tables. |
| `agent` | no | Explicit compute (an agent UUID); omit to let the API auto-pick. |

## Write dispositions

- **`append`** — stages Parquet and loads it into the table.
- **`replace`** — `insert-from-staging` (default) swaps via a staging dataset;
  `truncate-and-insert` clears the table first. Iceberg has no cheap `TRUNCATE`, so
  truncation is a `DELETE FROM … WHERE 1=1`.
- **`merge`** — delete-insert upsert against a staging dataset; set a `primary_key`
  (and/or `merge_key`). A second run with the same key updates in place rather than
  appending.

Tables are stored as **Iceberg** (via the attached Polaris catalog); the type mapper
constrains dlt types to Iceberg-safe DuckDB types (JSON → `VARCHAR`, microsecond
timestamps, no 128-bit integers). Schema evolution (new columns on a later run) is applied
with `ALTER TABLE … ADD COLUMN`.

### Reading values back

DuckHaven returns rows as JSON, so temporal values arrive as ISO-8601 strings and are
converted back to `datetime` on the way out. Which columns get converted is decided from
the column types the server reports, so a `VARCHAR` column that happens to hold an
ISO-8601-looking string stays the string it is. Against a server that reports no column
types the destination falls back to recognizing datetimes by their shape, which *can*
misread such a `VARCHAR` — the reason the typed path exists.

## Observability

With the `otel` extra, each load job and staging upload emits an OpenTelemetry span
(`dlt_duckhaven.load_job`, `dlt_duckhaven.stage`). Because the connector injects a W3C
`traceparent` on every request, those spans parent the connector's HTTP spans and the
server trace, so a dlt load traces end-to-end (client → API → agent). Without the extra the
instrumentation is a no-op.

## Governance & staging

Bulk Parquet is staged to the workspace object storage via a **presigned-URL stage** — the
session's `staging-files` endpoint returns a short-lived `put_url` (upload) and `get_url`
(read) per file, scoped to the session's staging prefix. The client uploads to `put_url`
with a plain HTTP PUT; the agent reads `get_url` over httpfs. **No storage credentials live
on the client or the agent** — all backend-specific signing (S3/MinIO SigV4, Azure SAS)
happens in the API, which already owns the storage integration. Every load statement is
authorized and audited at the API, so a dlt load is fully governed.

## License

Apache-2.0.
