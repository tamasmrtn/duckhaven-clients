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

> **Status: alpha.** The static surface (destination factory, capabilities, type mapping,
> configuration, registration) is in place. Staged loads (`append`/`replace`/`merge`) and
> the end-to-end pipeline are landing incrementally; see `CHANGELOG.md`.

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

## Governance & staging

Bulk Parquet is staged to the workspace object storage using a **per-load, API-vended,
scoped credential** — never a long-lived storage secret on the client. The load `COPY` is
read by the agent using its own catalog-scoped storage access. On the bundled MinIO backend
(which has no STS) the vended credential is prefix-scoped; true short-lived STS applies to
external S3.

## License

Apache-2.0.
