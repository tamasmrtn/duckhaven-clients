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
> (`append`/`replace`/`merge`), and schema evolution are implemented and unit-tested. The
> live end-to-end path is **gated on a server-side staging-credential endpoint** that is not
> yet shipped (`POST …/sql/sessions/{id}/staging-credentials`); until then a real pipeline
> run cannot complete. See `CHANGELOG.md`.

## Install

```sh
pip install "dlt-duckhaven[s3]"      # s3fs for the default MinIO/S3 staging backend
# pip install "dlt-duckhaven[az]"    # adlfs for external ADLS Gen2
# pip install "dlt-duckhaven[otel]"  # per-load-job spans
```

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

## Observability

With the `otel` extra, each load job and staging upload emits an OpenTelemetry span
(`dlt_duckhaven.load_job`, `dlt_duckhaven.stage`). Because the connector injects a W3C
`traceparent` on every request, those spans parent the connector's HTTP spans and the
server trace, so a dlt load traces end-to-end (client → API → agent). Without the extra the
instrumentation is a no-op.

## Governance & staging

Bulk Parquet is staged to the workspace object storage using a **per-load, API-vended,
scoped credential** — never a long-lived storage secret on the client. The load `COPY` is
read by the agent using its own catalog-scoped storage access. On the bundled MinIO backend
(which has no STS) the vended credential is prefix-scoped; true short-lived STS applies to
external S3.

## License

Apache-2.0.
