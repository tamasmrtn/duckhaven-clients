# Changelog

All notable changes to `dlt-duckhaven` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come from git tags
(`dlt-duckhaven-vX.Y.Z`).

## [Unreleased]

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
  stages a load's Parquet to the workspace object storage with a per-load API-vended
  credential (`_staging`) and issues `INSERT INTO … SELECT * FROM read_parquet(…)` through
  the session (the agent reads the staged files with its own access; no credential in the
  load SQL). **Live loading depends on the server-side staging-credential vend endpoint**;
  the end-to-end test stays gated until it ships.
- Write dispositions `replace` and `merge`: delete-insert merge and insert-from-staging /
  truncate-and-insert replace (Iceberg truncation via `DELETE FROM`, not `TRUNCATE`) via
  the staging dataset — inherited from the SQL job client and covered by tests.
- Schema evolution: `DuckHavenJobClient.get_storage_tables` introspects existing columns
  with `DESCRIBE` instead of `information_schema.columns`, which is unreliable for attached
  Iceberg (Polaris) catalogs. The exact DuckDB DESCRIBE type spellings are validated by the
  gated e2e (append → schema evolution → merge idempotency).
- Optional OpenTelemetry spans (`otel` extra): each load job and staging upload emits a
  span (`dlt_duckhaven.load_job`, `dlt_duckhaven.stage`) that parents the connector's HTTP
  spans, so a dlt load traces end-to-end. No-op when the extra is absent.
