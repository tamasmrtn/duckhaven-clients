# Changelog

All notable changes to `duckhaven-sql-connector` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come from git tags
(`sql-connector-vX.Y.Z`).

## [Unreleased]

### Changed

- `agent_forbidden` — the error a DuckHaven server returns when the caller may see an agent
  but holds too low a per-agent tier to run on it — is now mapped to `ProgrammingError` by
  its slug rather than by its status code. It already landed there via the 403 default, so
  nothing changes today; the mapping now holds if the server ever sends the slug on a
  status that defaults elsewhere (a 409 would otherwise read as reconnect-and-retry, the
  wrong advice for an access denial).

### Documentation

- The README now describes the two shapes an agent-access denial takes at `connect()`: a
  403 `agent_forbidden` when the agent is visible but your tier is too low, and a bare 404
  `Agent not found` when the agent is restricted and you hold no grant — the server hides
  such an agent rather than forbidding it, so it is indistinguishable from a deleted one.
  Also notes that omitting `agent` restricts auto-pick to agents you may use, which can
  report no agent available where an unrestricted deployment would have connected.

## [0.3.0] - 2026-07-23

### Added

- `Connection.server_version()` reads `GET /api/version`, returning a `ServerVersion(version,
  api_version)` — the release/build version and the integer API-contract version — or `None`
  against a server predating the endpoint (404). Session-independent, so it still answers
  after the session has gone dead. Provenance and coarse compatibility for support and
  diagnostics; `api_version` moves only on a breaking change, so it is not a feature flag.
- `Cursor.description` now reports each result column's type in PEP 249's `type_code`
  field, spelled the way DuckDB prints a logical type (`DECIMAL(18,4)`,
  `TIMESTAMP WITH TIME ZONE`, `STRUCT(a INTEGER, b VARCHAR)`). `Cursor.column_types`
  exposes the same list on its own. Both are `None` against a server or agent that does not
  report types, which is what `type_code` always was before, so existing readers are
  unaffected. Values are deliberately **not** cast to the declared type: results travel as
  JSON, so `DECIMAL` and `HUGEINT` have already lost precision and casting would hide that
  rather than fix it.

### Changed

- **Breaking:** `Cursor.columns()` now requires an exact `table_name` and raises
  `ProgrammingError` without one (or given a `%` pattern). It reports columns with
  `DESCRIBE` instead of `information_schema.columns`, which cannot introspect an attached
  Iceberg table — it returns a single `__`/`UNKNOWN` placeholder row, and *inconsistently*,
  so the previous implementation returned wrong columns non-deterministically with no
  error. Enumerate with `tables()`, then call `columns()` per relation. The returned row
  shape is unchanged.
- `Cursor.catalogs()`, `schemas()` and `tables()` now read DuckHaven's REST browse
  endpoints instead of `information_schema`. Engine-side enumeration is rejected outright
  on any workspace with a scoped catalog attached — including for sessions whose active
  catalog is open — because the engine cannot filter those listings by grant. The browse
  endpoints can, and behave identically on open catalogs. Same methods, same row shapes,
  same `LIKE` filtering; they now cost one request per catalog in scope, plus one per
  schema for `tables()`, so pass `catalog=`/`schema_name=` where you can.
- The `User-Agent` now leads with the calling application (`application=`) rather than
  appending it. DuckHaven attributes a session from the *first* product token, so dbt and
  dlt sessions were previously all recorded as `duckhaven-sql-connector`.

## [0.2.0] - 2026-07-19

### Added

- `Connection.stage_files(names)` → `StagingFiles(files=[StagedFile(name, key, put_url,
  get_url)], expires_at)`: presigns a PUT (upload) and GET (read) URL per file under a
  session's stage (`POST …/sql/sessions/{id}/staging-files`), used by the dlt `duckhaven`
  destination. The client uploads bulk data to `put_url` with a plain HTTP PUT and the
  agent reads `get_url` over httpfs — no storage credentials on either side.

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
