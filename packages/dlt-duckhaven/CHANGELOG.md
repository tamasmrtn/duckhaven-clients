# Changelog

All notable changes to `dlt-duckhaven` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come from git tags
(`dlt-duckhaven-vX.Y.Z`).

## [Unreleased]

## [0.1.0] - 2026-07-19

### Added

- Destination factory `duckhaven` (registered so `destination="duckhaven"` resolves), its
  `DestinationCapabilitiesContext` profile (staged Parquet, Iceberg table format, DuckDB
  identifier/literal escaping, autocommit — no DDL transactions), and the DuckHaven→Iceberg
  `DuckHavenTypeMapper` (JSON→VARCHAR, microsecond timestamps, HUGEINT rejected).
- `DuckHavenClientConfiguration` / `DuckHavenCredentials` (`host`/`workspace`/`agent`/
  `catalog` + a `dh_pat_…` token), mirroring the DuckHaven session-API config shape.
- Load path (append): `DuckHavenSqlClient` (opens a session via the connector, drives
  statements through the session cursor, qualifies `catalog.schema.table`, maps connector
  errors to dlt errors), `DuckHavenJobClient` (staging-dataset-aware SQL job client + the
  `insert_values` fallback + `SupportsStagingDestination`), and `DuckHavenCopyJob` — which
  presigns each load file via the session (`stage_files` →
  `POST …/sql/sessions/{id}/staging-files`), uploads the local Parquet to the returned
  `put_url` with a plain HTTP PUT (`_staging`), and issues
  `INSERT INTO … SELECT * FROM read_parquet('<get_url>')` through the session. The agent
  reads the presigned `get_url` over httpfs — no storage credentials on the client or the
  agent, and no per-backend storage SDK on the client.
- Write dispositions `replace` and `merge`: delete-insert merge and insert-from-staging /
  truncate-and-insert replace (Iceberg truncation via `DELETE FROM`, not `TRUNCATE`) via
  the staging dataset — inherited from the SQL job client and covered by tests.
- Schema evolution: `DuckHavenJobClient.get_storage_tables` introspects existing columns
  with `SELECT * FROM (DESCRIBE …)` (wrapped in a SELECT so the session can materialize the
  result) instead of `information_schema.columns`, which is unreliable for attached Iceberg
  (Polaris) catalogs.
- Coerce ISO-8601 timestamp strings in results to `datetime` in `DuckHavenSqlClient`: the
  results API returns untyped JSON, and dlt expects datetime objects for timestamp columns
  (e.g. `_dlt_version.inserted_at`). A typed/Arrow result disposition on the server would
  remove the need for this.
- Validated end-to-end against a live DuckHaven (append → schema evolution → merge
  idempotency) via the presigned-URL stage.
- Optional OpenTelemetry spans (`otel` extra): each load job and staging upload emits a
  span (`dlt_duckhaven.load_job`, `dlt_duckhaven.stage`) that parents the connector's HTTP
  spans, so a dlt load traces end-to-end. No-op when the extra is absent.
