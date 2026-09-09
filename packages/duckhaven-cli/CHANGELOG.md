# Changelog

All notable changes to `duckhaven-cli` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come from git tags
(`duckhaven-cli-vX.Y.Z`).

## [Unreleased]

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
