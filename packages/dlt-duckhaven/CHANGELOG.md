# Changelog

All notable changes to `dlt-duckhaven` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come from git tags
(`dlt-duckhaven-vX.Y.Z`).

## [Unreleased]

### Fixed

- `has_dataset()` no longer queries `INFORMATION_SCHEMA.SCHEMATA` to check whether the
  destination schema exists. That query is engine-side enumeration, which DuckHaven rejects
  (403) once *any* catalog in the workspace is attached scoped — the denial applies to every
  session in that workspace, so a load into an unrelated open catalog could fail too. It now
  reads the workspace catalog API via the connector's `schemas()` cursor method instead, the
  same approach `get_storage_tables` already used for column introspection.

## [0.3.0] - 2026-08-01

### Changed

- Require `duckhaven-sql-connector>=0.4.0`: the cold-start behaviour below is entirely that
  version's doing. Against an older connector a pipeline still fails `open_connection` on an
  idle elastic deployment, so the floor is what makes the claim true rather than aspirational.
- A pipeline run against a DuckHaven deployment whose elastic compute has scaled to zero now
  waits for an agent to start rather than failing `open_connection`. This comes from
  `duckhaven-sql-connector` and needs no config change; the wait is bounded (five minutes by
  default).

### Documentation

- The `agent` config option now documents what a per-agent access denial looks like from a
  pipeline. Both shapes fail `open_connection`, so the load stops before any job runs:
  *"Agent not found"* on an agent you know exists means it is restricted and you hold no
  grant (such an agent is hidden, not reported as forbidden, so it reads identically to a
  deleted one), while *"requires the 'use' tier"* means it is visible to you but your grant
  is too low. Omitting `agent` narrows auto-pick to agents you may use rather than falling
  back to one you cannot run on.

## [0.2.0] - 2026-07-23

### Changed

- Require `duckhaven-sql-connector>=0.3.0`: the timestamp-coercion fix below reads the
  result column types that connector version reports (`description` `type_code`). Against
  an older connector the value carries no type and the destination falls back to the
  previous shape-based heuristic.

### Fixed

- A `VARCHAR` column holding an ISO-8601-looking string (e.g. `"2024-05-06T07:08:09Z"`) is
  no longer silently converted to a `datetime` on the way to the destination. Timestamp
  coercion now follows the column types the server reports rather than matching each
  value's shape against a regex. Against a server that reports no types the previous
  shape-based behaviour is kept unchanged, so nothing regresses on an older deployment.

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
