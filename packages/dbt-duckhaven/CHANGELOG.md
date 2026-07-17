# Changelog

All notable changes to `dbt-duckhaven` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-07-17

### Added

- Initial `dbt-duckhaven` adapter: registers `type: duckhaven`, subclasses `dbt-duckdb`,
  and routes every statement through the DuckHaven session API via
  `duckhaven-sql-connector`. Materializations: table, seed, incremental, ephemeral, and
  snapshots. Views and Python models are not supported.
- `merge` and `microbatch` incremental strategies, gated on the agent's DuckDB being
  ≥ 1.5.3 — the release where `duckdb-iceberg` gained `MERGE INTO` for Iceberg tables.
  dbt-duckdb's own gate is 1.4.0-dev0, a core-DuckDB gate that would advertise `merge` on
  an agent that then fails mid-run.
- `duckhaven__get_incremental_merge_sql` emits dbt-core's explicit
  `UPDATE SET` / `INSERT … VALUES` MERGE instead of dbt-duckdb's `UPDATE BY NAME` /
  `INSERT BY NAME`, which duckdb-iceberg does not document. dbt-duckdb's DuckDB-only merge
  configs (`merge_clauses`, `merge_returning_columns`, `merge_on_using_columns`,
  `merge_update_condition`, `merge_insert_condition`, `merge_update_set_expressions`) now
  raise a compile-time error rather than being silently dropped.
- Snapshots: `duckhaven__snapshot_merge_sql` uses the documented Iceberg `MERGE INTO`
  instead of dbt-duckdb's joined `UPDATE … FROM`, and snapshot staging goes back to
  dbt-core's session-local temp table (dbt-duckdb makes it a real table for MotherDuck,
  which on DuckHaven would land an unqualified Iceberg table in the session's default
  namespace).
- `duckhaven__drop_relation` drops without `CASCADE` (the Iceberg REST catalog rejects it,
  as it already does for `DROP SCHEMA`); the `table` materialization hits this on rebuild.
- Custom `table` materialization: rebuilds a table by dropping and recreating it in place
  rather than dbt-duckdb's `__dbt_tmp` build-and-`ALTER … RENAME` swap. Iceberg rename keeps
  the old storage location, so the swap left the table at the `__dbt_tmp` path and the next
  run's `CREATE … __dbt_tmp` failed with a Polaris location conflict.
- `dbt run` identifies dbt to the server via the connector User-Agent
  (`dbt-duckhaven/<version>`), for governed attribution.
- Query cancellation: aborting a run (Ctrl-C / `--fail-fast`) cancels each session's
  in-flight statement, freeing the agent's admission slots instead of leaving abandoned
  queries to run to completion. `is_cancelable()` now returns `True`.
- `profile_template.yml` so `dbt init` scaffolds the connection prompts.

### Changed

- `duckhaven-sql-connector` dependency now requires `>=0.1.0`, its first published release,
  instead of an unpinned floor.
- `catalog` is now a required profile field (previously it silently fell back to an
  in-memory sentinel catalog).
- `CONSTRAINT_SUPPORT` reflects Iceberg: only `not_null` is enforced; `primary_key` /
  `foreign_key` / `unique` are not enforced and `check` is unsupported (was all-enforced,
  inherited from dbt-duckdb).
- `MicrobatchConcurrency` capability is no longer advertised as fully supported. Microbatch
  itself is supported; concurrent batches are not, because each would take its own session
  (an agent admission slot) and race the others to commit to the same Iceberg table.

### Fixed

- `get_column_schema_from_query` wraps its `DESCRIBE` in a `select * from (describe (…))`.
  dbt-duckdb issues a bare `DESCRIBE (<sql>)`; DuckDB reports DESCRIBE as a SELECT, so the
  agent materializes it with `COPY (<sql>) TO … (FORMAT PARQUET)` — and `COPY (DESCRIBE …)`
  is a parser error. Snapshots reach this on every run, so `dbt snapshot` failed outright.
