# Changelog

All notable changes to `duckhaven-sql-connector` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come from git tags
(`sql-connector-vX.Y.Z`).

## [Unreleased]

### Added

- `Connection.vend_staging_credentials()` → `StagingCredentials(uri, credentials,
  expires_at)`: vends per-load, scoped, short-lived credentials to stage bulk files under
  a session's `staging_uri` (`POST …/sql/sessions/{id}/staging-credentials`), used by the
  dlt `duckhaven` destination. **Depends on a server endpoint not yet in the pinned
  contract subset**; extend `contract/` + `scripts/refresh_contract.py` once the server
  ships it.

## [0.1.0] - 2026-07-17

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
- Cursor metadata methods (`catalogs`/`schemas`/`tables`/`columns`) over
  `information_schema`, for dbt/BI relation introspection.
- Connection-scoped `Connection.cancel()` that cancels the session's in-flight statement
  (for drivers like dbt that abort a run from another thread); the statement's id is now
  recorded before polling, so a cancel arriving mid-run reaches the running statement.
- Retry hardening: honors a server `Retry-After` header and bounds retries by a total-time
  budget (`RetryPolicy.max_elapsed`, raising `MaxRetryDurationError`).
- A pinned OpenAPI contract subset with an anti-drift test, env-gated live integration
  tests, and a quickstart example.
- Workspace and package scaffolding: uv workspace, Apache-2.0 license, Ruff/pre-commit,
  CI matrix (Python 3.10–3.14), and the tag-prefixed release workflow.
