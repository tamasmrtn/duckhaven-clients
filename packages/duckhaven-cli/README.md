# duckhaven-cli

`dh` — the command-line interface for [DuckHaven](https://github.com/tamasmrtn/duckhaven).

It gives analysts, CI pipelines and operators scriptable access to DuckHaven's REST API:
running SQL, browsing the catalog, publishing dbt lineage and semantic models, applying
dashboards from git, managing grants, and operating service accounts, users and agents.

Like every member of this repo it is a **pure HTTP client of DuckHaven's public REST
API**, authenticating with a Personal Access Token (`dh_pat_…`). The interactive REPL
holds a SQL session through
[`duckhaven-sql-connector`](../duckhaven-sql-connector/README.md); everything else,
including one-shot `dh sql`, goes over the REST API directly, because the one-shot query
route works on deployments where SQL sessions are switched off.

## Install

```sh
pip install duckhaven-cli
# or, to get an isolated tool install:
uv tool install duckhaven-cli
```

## Getting started

```sh
dh auth login --host https://duckhaven.example.com
dh sql -q "select 1"
```

## The result cache

DuckHaven answers a repeated read from its result cache when nothing the query reads has
changed. `dh sql` says so on stderr when it happens; pass `--no-cache` to run the query
anyway (also on `dh session open` and `dh session exec`). An owner can switch the cache off
for a whole workspace with `dh workspace update --no-result-cache`. The interactive REPL
does not take `--no-cache`: it holds its session through the connector, which has its own
`use_cache` option, and the shell does not expose it yet.

## Newer server features

A server that supports them offers: DuckLake catalogs (`dh catalog kinds`,
`dh catalog create --kind ducklake`, `dh catalog rotate-role`, `dh catalog export`), the
DuckDB runtimes agents run (`dh agent runtimes`, `--runtime` on
`dh admin agent elastic-create`), per-agent monitoring over a window or range and the
queries behind it (`dh admin agent monitoring`, `dh admin agent queries`), applying
maintenance recommendations (`dh admin maintenance recommendations|apply`), and your
private editor tabs (`dh worksheet`). Against an older server these commands answer with the
server's own not-found or validation error.

## Dashboards in git

A DuckHaven dashboard is one YAML file. Keep each one in a repository as
`<name>.dashboard.yml`, named after the `name` inside it, review changes in pull
requests, and let CI apply them. `dh dashboard` is that CI step:

```sh
# On a pull request: check every file as publishing would, and change nothing.
dh dashboard apply dashboards/ --dry-run --publish

# On main: apply and publish each file, and remove the dashboards whose files were deleted.
dh dashboard apply dashboards/ --publish --prune --yes \
  --source-id "$GITHUB_REPOSITORY" --source-ref "$GITHUB_SHA"
```

`apply` sends each file byte for byte, so DuckHaven keeps it as written, comments included,
and reports a problem at the line its author wrote: `dashboards/sales.dashboard.yml:12:16:
error: ...`. A dashboard applied this way is managed in git: DuckHaven refuses every other
change to it until it is detached. Without `--publish` a file becomes the dashboard's draft
and readers keep the published one; publishing needs a workspace `owner`, but a dry run
needs only `writer`, so a pull request's check can run with a token that cannot publish. Every
file is applied and every failure reported, not only the first, and the exit status is the
first failure's: `5` for a file the server refused, such as `source_changed` (another
repository manages that dashboard) or `managed_dashboard`.

Applying never deletes a dashboard. `--prune` does, for the dashboards applied from
`--source-id` that no file names any more: it lists them and asks before anything is applied
(or takes `--yes`), and it deletes nothing when any file failed to apply.

The other commands:

- `validate` checks files against the workspace without storing anything: a metric that
  does not exist, or a model that is not published.
- `rename OLD FILE` renames a dashboard whose file you renamed. Run it once before merging:
  applying a renamed file as usual creates a second dashboard. The old name keeps working in
  links.
- `adopt NAME DIR` takes over a dashboard made in DuckHaven: it saves the dashboard's file
  into `DIR` and applies it, publishing nothing. Commit the file.
- `detach`, `publish`, `delete`, `list` and `get`, each one request.
- `export NAME` prints a dashboard's file and `schema` the JSON Schema for your editor. Both
  print the file itself rather than the usual JSON envelope, so
  `dh dashboard export sales > dashboards/sales.dashboard.yml` works.

`dh` does not read or rewrite the files; there is no formatter yet. These commands need a
DuckHaven that offers dashboards; an older one answers with its own not-found error.

## Exact numbers

A DuckHaven with `api_version` 3 or later sends `DECIMAL` values and 64-bit-or-wider integers
(`BIGINT`, `HUGEINT`, `count(*)`) as strings, because JSON numbers lose precision past about
16 digits. `dh` prints them exactly as before in table and CSV output. With `--format json`
they stay strings (`"12.50"`, `"9007199254740993"`), exact. Scripts reading `dh` JSON should
convert those columns themselves rather than expect JSON numbers.

## Documentation

- [Command-line quickstart](https://tamasmrtn.github.io/duckhaven/getting-started/cli-quickstart/) — install, sign in,
  run a query
- [CLI reference](https://tamasmrtn.github.io/duckhaven/reference/cli/) — every command and flag

## Licence

Apache-2.0. See [LICENSE](LICENSE).
