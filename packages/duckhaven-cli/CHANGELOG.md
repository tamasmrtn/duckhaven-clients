# Changelog

All notable changes to `duckhaven-cli` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions come from git tags
(`duckhaven-cli-vX.Y.Z`).

## [Unreleased]

### Added

- `dh workspace update --timezone` sets the workspace's IANA time zone, such as `Europe/London`.
  Relative windows like month to date resolve in it. Owner only. The server checks the name and
  refuses an unknown one with a conflict. A DuckHaven older than v0.11.0 ignores the flag silently.
- `dh audit list` reads the access audit log, newest first: access changes, sign-ins and
  refused requests, in every workspace. It filters by time, category, action or action
  prefix, actor, subject, workspace (`--in-workspace`), target, outcome and `--self-change`,
  and takes `--limit` and `--all`. It needs the `audit:read` permission and a DuckHaven newer
  than v0.10.0; an older server answers `404`.

### Changed

- `dh query list`'s help calls it the query log only. The audit log is `dh audit list`.
- `dh workspace create` says it needs `workspaces:create`, held by the admin role only. Against a
  DuckHaven at v0.11.0 or later a `user` account gets `403`, which exits with code 3. Older servers
  let any signed-in account create a workspace.
- `dh catalog attach` says it needs workspace ownership and the catalog's creator or
  `catalogs:admin`. `dh catalog create --storage-backend` and `dh catalog export create` say they
  need `storage:manage`.

### Fixed

- `dh search --type` lists every kind of object DuckHaven searches: `dashboard` and
  `semantic_model` as well as `catalog`, `schema`, `table` and `saved_query`. A server at
  v0.9.0 or earlier finds only the first four and answers `422` when asked for the others.

## [0.5.0] - 2026-11-15

### Added

- **Dashboards in git.** `dh dashboard apply` applies `<name>.dashboard.yml` files, or every
  one under a directory, to the dashboards they are named after, byte for byte. It takes
  `--publish`, `--dry-run`, `--source-id`, `--source-ref`, `--expected-hash` and `--force`,
  applies every file and reports each problem as `file:line:col`, and exits with the first
  failure's status. `--prune` detaches and deletes the dashboards applied from
  `--source-id` whose files are gone, after asking (or with `--yes`), and only when every
  file applied.
- `dh dashboard validate`, `rename`, `adopt`, `detach`, `publish`, `delete`, `list` and
  `get`. `dh dashboard export` prints a dashboard's YAML and `dh dashboard schema` its JSON
  Schema, each as the file itself rather than the JSON envelope.
- `dh dashboard test` runs dashboard files without saving them and reports every widget as
  `ok`, `empty`, `error` or `no_access`, with its time. It exits `6` when a widget fails or
  cannot be read, and marks widgets slower than `--slow` seconds. It takes `--no-cache`.
- `dh dashboard preview FILE` holds a file as a preview, prints its page's address, and
  sends the file again each time it is saved, starting a new preview when the last one has
  ended. `--open` opens the page. `--once` runs the file once and prints each widget's state,
  rows and SQL instead, which suits a coding agent checking its work.
- `dh dashboard impact REF` lists the dashboards that read a semantic model, metric or
  dimension.
- `dh dashboard exposures MANIFEST` prints dbt exposures for the published dashboards that
  read a dbt project's models, seeds, snapshots and sources, with `--owner-name` and
  `--owner-email` for the owner dbt requires.
- `dh semantic import --dry-run` reports what an import would create, update and remove, and
  the published dashboards it would break. `--allow-breaking` on `dh semantic import`,
  `purge` and `deprecate` makes a change that breaks published dashboards; without it, the
  change is refused and every dashboard it breaks is listed.
- The README has an `AGENTS.md` snippet for coding agents that write dashboard files.

### Changed

- The pinned API contract is refreshed and covers the dashboard endpoints, including
  previews and their runs.

## [0.4.0] - 2026-10-15

### Changed

- `dh sql` and the REPL ask the server to hold the submit for up to two seconds, so a quick
  query finishes in one request instead of a submit and a poll. `--no-wait` still returns at
  once. A server that predates the hold ignores it.
- Ready for DuckHaven `api_version` 3: `DECIMAL` and wide integers, which it sends as
  strings, print as before in table and CSV output and stay exact strings with
  `--format json`.

## [0.3.0] - 2026-10-13

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
