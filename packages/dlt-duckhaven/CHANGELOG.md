# Changelog

All notable changes to `dlt-duckhaven` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come from git tags
(`dlt-duckhaven-vX.Y.Z`).

## [Unreleased]

### Fixed

- The post-run summary (`str(load_info)`) now shows the destination's `host/workspace`
  instead of a masked token (e.g. `The duckhaven destination used dh_pat_*** location to
  store data`). `DuckHavenClientConfiguration` previously inherited dlt-core's default
  `__str__`, which displays `str(credentials)` — informative for connection-string-based
  destinations, but for DuckHaven's bearer-token credentials that's just a masked
  placeholder. `__str__` now returns `physical_location()` (`host/workspace`), falling
  back to the masked credentials form when `host`/`workspace` aren't set.

## [0.5.0] - 2026-08-05

### Fixed

- Schema evolution that adds two or more columns to an existing table no longer fails.
  The destination was emitting dlt's default combined `ALTER TABLE … ADD COLUMN a, ADD
  COLUMN b`, which DuckDB rejects outright — *"Parser Error: Only one ALTER command per
  statement is supported"* — even though each column on its own is an ordinary schema
  change. A table could therefore run for months needing only single-column adds and then
  break the first time two arrived in the same load, which made it a real hazard for
  sources with data-dependent schema drift (a field that is null-only in one load and
  typed in the next). Columns are now added one statement at a time. If you worked around
  this by pre-declaring the columns as explicit `columns` hints on the resource, those
  hints are still perfectly valid and can stay; they are simply no longer required.
- A load no longer aborts when two of its jobs commit to the same Iceberg table at once.
  dlt runs load jobs in parallel (20 workers by default), and one resource whose data
  spans several Parquet files is enough to produce two jobs for the same table; Iceberg
  settles that race with optimistic concurrency and Polaris rejects the loser with a 409.
  The destination classified that rejection as a terminal error, so instead of being
  retried the job failed and took the whole load package with it. A lost commit race is
  now treated as what it is — transient — and the statement is retried with jittered
  backoff against the refreshed table metadata. The retry sits at the SQL client, so it
  covers every commit a load makes, not just the data ones: `replace`'s `DELETE FROM`, the
  merge SQL, and the end-of-load bookkeeping writes to `_dlt_loads`/`_dlt_version`, which
  happen outside any load job and so would otherwise fail the run outright with no retry
  at all. It is safe because a rejected commit publishes no metadata: none of its rows are
  visible, so re-running cannot duplicate them. The `LOAD__WORKERS=1` workaround, which
  serialized *every* table's jobs to avoid a race between two of them, is no longer
  needed. A concurrent `CREATE TABLE` that loses is deliberately *not* retried — it fails
  with `AlreadyExistsException`, which retrying can never resolve.

## [0.4.0] - 2026-08-01

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
