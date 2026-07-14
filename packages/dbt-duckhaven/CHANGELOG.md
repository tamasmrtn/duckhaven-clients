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
