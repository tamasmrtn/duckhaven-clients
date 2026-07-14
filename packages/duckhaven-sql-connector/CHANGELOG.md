# Changelog

All notable changes to `duckhaven-sql-connector` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come from git tags
(`sql-connector-vX.Y.Z`).

## [Unreleased]

### Added

- Initial DB-API 2.0 (PEP 249) client for DuckHaven's SQL session API: `connect`,
  `Connection` (one SQL session), `Cursor` (submit/poll/fetch), the exception hierarchy,
  module globals (`paramstyle = "qmark"`), and type objects.
- Pooled `httpx` transport with PAT bearer auth, idempotent-only retry/backoff, and
  HTTP-status/error-body → DB-API exception mapping.
- Safe client-side `qmark` parameter binding and cursor-paginated JSON results, behind a
  result-transport seam ready for a future server-side Arrow/EXTERNAL_LINKS disposition.
- Optional extras: `arrow` (`Cursor.fetch_arrow_table`) and `otel` (client spans + W3C
  `traceparent` propagation), plus dependency-free instrumentation `Hooks`.
- A pinned OpenAPI contract subset with an anti-drift test, env-gated live integration
  tests, and a quickstart example.
- Workspace and package scaffolding: uv workspace, Apache-2.0 license, Ruff/pre-commit,
  CI matrix (Python 3.10–3.14), and the tag-prefixed release workflow.
