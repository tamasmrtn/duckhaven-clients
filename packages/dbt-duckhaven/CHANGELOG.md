# Changelog

All notable changes to `dbt-duckhaven` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Initial `dbt-duckhaven` adapter: registers `type: duckhaven`, subclasses `dbt-duckdb`,
  and routes every statement through the DuckHaven session API via
  `duckhaven-sql-connector`. v1 materializations: table, view, seed, incremental,
  ephemeral. Python models and snapshots are not supported.
- `duckhaven__drop_relation` drops without `CASCADE` (the Iceberg REST catalog rejects it,
  as it already does for `DROP SCHEMA`); the `table` materialization hits this on rebuild.
- `dbt run` identifies dbt to the server via the connector User-Agent
  (`dbt-duckhaven/<version>`), for governed attribution.
- `profile_template.yml` so `dbt init` scaffolds the connection prompts.

### Changed

- `catalog` is now a required profile field (previously it silently fell back to an
  in-memory sentinel catalog).
- `CONSTRAINT_SUPPORT` reflects Iceberg: only `not_null` is enforced; `primary_key` /
  `foreign_key` / `unique` are not enforced and `check` is unsupported (was all-enforced,
  inherited from dbt-duckdb).
- `MicrobatchConcurrency` capability is no longer advertised as fully supported (avoids
  oversubscribing agent sessions; microbatch is not a v1 materialization).
