# Changelog

All notable changes to `duckhaven-cli` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come from git tags
(`duckhaven-cli-vX.Y.Z`).

## [Unreleased]

Brings the CLI up to date with the DuckHaven server's result cache, DuckLake catalogs,
agent runtimes, worksheets and reshaped monitoring. Everything new needs a server that
offers it; against an older one the command fails with that server's own error.

### Added

- **Result cache.** `--no-cache` on `dh sql`, `dh query run`, `dh session open` and
  `dh session exec`, and `dh workspace update --result-cache/--no-result-cache` (owner
  only). When a query was answered from the cache, `dh sql` and `dh session exec` say so on
  stderr. The REPL is not cache-aware.
- **Catalogs.** `dh catalog create --kind` (`iceberg_polaris` or `ducklake`),
  `dh catalog kinds`, `dh catalog rotate-role`, and `dh catalog export list|create|cancel`
  for copying a DuckLake catalog out as Iceberg. `dh admin catalog reconcile-roles` creates
  any DuckLake database role that is missing (idempotent, deployment-wide).
- **Runtimes.** `dh agent runtimes`, and `--runtime` / `--allow-beta` on
  `dh admin agent elastic-create`.
- **Monitoring.** `dh admin agent monitoring` takes `--window` or `--start`/`--end`, and
  `dh admin agent queries` lists the runs behind a time range, sortable by wait, duration,
  peak memory, CPU time, spill or bytes read.
- **Maintenance.** `dh admin maintenance recommendations` and `dh admin maintenance apply`
  (asks first; needs `maintenance:manage`).
- **Worksheets.** `dh worksheet list|get|create|update|delete`. Changing a worksheet's
  title or SQL requires `--base-version`; a concurrent edit is refused with exit `5` and
  the current worksheet in the error's details.
- `dh saved-query create --on-conflict error` fails with exit `5` instead of overwriting
  a query with the same name, and `dh search --type` limits the kinds of object searched.

### Changed

- With no agent named, `dh sql` and `dh session open` no longer list agents first and leave
  the choice to the server, which picks a connected agent on the default runtime. Before,
  an account that could see several agents sent no choice and a server without elastic
  compute refused with `agent_required`.
- Status polls ask the server to hold the call until the query finishes, and a query the
  submit response already reports as finished is not polled at all.
- The pinned API contract is refreshed, and now also checks the query parameters the new
  commands send.

## [0.2.0] - 2026-09-09

### Changed

- Drop support for Python 3.10; the minimum supported version is now 3.11.

## [0.1.1] - 2026-09-09

### Fixed

- Declare `click` as a direct dependency. `dh` failed to start with
  `ModuleNotFoundError: No module named 'click'` when installed standalone:
  `context.py`/`repl.py` catch `click.Abort` directly, but `typer>=0.27` no longer
  depends on `click` itself, and the workspace dev environment masked this because
  other packages pulled `click` in transitively.

## [0.1.0] - 2026-09-09

### Added

- `dh`, the DuckHaven command-line interface.
- **Running SQL.** `dh sql` submits, polls to a terminal state and fetches every page
  of results; `--no-wait` returns the id instead. Ctrl-C cancels the query on the
  server rather than orphaning it, and a query that runs and fails exits `6` so a
  pipeline can tell bad SQL from a broken CLI. `dh sql` with no arguments on a
  terminal opens a REPL, session-backed where the server allows it.
- **The dbt path.** `dh lineage import` and `dh semantic import` publish build
  artifacts, including the `manifest.json` + `catalog.json` pair that unlocks
  column-level lineage, plus `validate`/`publish` to promote imported drafts.
- **Catalog and access.** `dh workspace`, `dh catalog`, `dh schema`, `dh table` and
  `dh grant`, with catalog resolution and `catalog.schema.table` references.
- **History.** `dh query list` with the full server-side filter set, `dh saved-query`,
  `dh schedule` and `dh search`.
- **Operators.** A thin `dh admin` over service accounts, tokens, users and agents,
  and `dh api` for anything without a hand-written command.
- **Auth.** `dh auth login` signs in and mints a personal access token, storing only
  the token at mode 0600 and refusing a config other users can read.
- Output as `--format json|table|csv` behind one stable envelope
  (`{data, cursor, has_more}`), defaulting to a table on a terminal and JSON when
  piped. Errors are JSON too when JSON was asked for.
