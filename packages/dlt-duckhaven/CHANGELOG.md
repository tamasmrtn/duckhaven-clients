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
