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
maintenance recommendations (`dh admin maintenance recommendations|apply`), your
private editor tabs (`dh worksheet`), and the access audit log (`dh audit list`). Against an
older server these commands answer with the server's own not-found or validation error.

## Who may do what

Some commands need a permission your account may not hold, and the server refuses them with
`403`, which `dh` reports with exit code 3 and the server's message. Three are worth knowing:

- `dh workspace create` needs `workspaces:create`, which only the `admin` role holds. A `user`
  account, or a token acting for one, gets `403`. Ask an admin to create the workspace, and
  you become its owner only if the admin adds you as one.
- `dh catalog attach` needs workspace ownership and the catalog's creator or `catalogs:admin`.
  Owning a workspace alone does not let a catalog in.
- `dh catalog create --storage-backend` and `dh catalog export create` need `storage:manage`.
  Without it, a new catalog goes on DuckHaven's bundled store.

Against a DuckHaven older than v0.11.0, `workspace create` is open to every signed-in account.

## The audit log

`dh audit list` reads DuckHaven's access audit log, newest first: every access change, sign-in
and refused request, in every workspace. It needs the `audit:read` permission, which the
`admin` role holds. Narrow it with `--since`, `--until`, `--category`, `--action` (a name, or a
prefix such as `catalog_grant.*`), `--actor`, `--subject`, `--in-workspace`, `--outcome` and
`--self-change`:

```sh
dh --format json audit list --category access --since 2026-11-01T00:00:00Z --all
```

The log covers the whole deployment, so the workspace your profile sets does not narrow it;
`--in-workspace` does.

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
- `test PATH...` runs each file once without saving it, with your grants, and reports every
  widget: `ok`, `empty`, `error`, or `no_access` when you may not read what it reads, with
  how long it took to arrive. It exits `6` when a widget fails or cannot be read, so a pull
  request catches a query that checks out but does not run. A widget slower than `--slow`
  seconds (5 by default) is marked slow, which does not fail the run. `--no-cache` runs every
  query rather than answering from the result cache.
- `impact REF` lists the dashboards that read a model, or a metric or dimension of one
  (`orders.revenue`): what renaming or removing it would touch.
- `exposures MANIFEST` prints [dbt exposures](https://docs.getdbt.com/docs/build/exposures)
  for the published dashboards that read your dbt project's tables, so dbt's lineage shows
  which dashboards read each model. Keep the file in the project and write it again when
  dashboards change:
  `dh --output models/duckhaven_exposures.yml dashboard exposures target/manifest.json`. Each
  exposure has the dashboard's title, description and link, and depends on the models, seeds,
  snapshots and sources it reads, through its semantic models' datasets or its SQL. A table
  matches a dbt relation when its catalog, schema and name are the relation's database,
  schema and alias, as when DuckHaven imports from dbt. dbt requires an owner:
  `--owner-name` (DuckHaven by default) and `--owner-email`.
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

Changes to the semantic layer are checked against dashboards too. An import,
`dh semantic purge` or `dh semantic deprecate` that removes something a published dashboard
reads is refused with exit `5`, listing every dashboard and what it reads, unless you pass
`--allow-breaking`. `dh semantic import --dry-run` reports what an import would create,
update and remove, and the dashboards it would break, and changes nothing.

## Writing a dashboard

`dh dashboard preview FILE` shows a file in DuckHaven while you edit it, without saving it.
It prints the address of a page that only you can see; open it beside your editor, or pass
`--open`. Each time you save the file, `dh` sends it, and the page runs it again, with your
grants. A file that does not load is reported at its line, and the page keeps showing the
last file that did. A metric that does not exist shows on the page as that widget's error.
Stop with Ctrl+C.

A preview ends two hours after its file last changed or it last ran. A save after that
starts a new preview, at a new address, which `dh` prints. The page is new in DuckHaven:
against a server without it, use `--once`. If your browser is signed out, sign in, then open
the address again, as signing in does not bring you back to it.

`--once` runs the file in the terminal instead, once, and prints each widget with its state,
rows and SQL. It exits as `test` does.

### For coding agents

An AI coding agent that writes dashboard files can check its own work the same way. Put
something like this in the repository's `AGENTS.md`, and give the agent a token with the
`writer` role, which `validate` and `preview` need:

```markdown
## Dashboards

Dashboards live in `dashboards/`, one `<name>.dashboard.yml` file each, named after the
`name` inside it. To write or change one:

1. Read the file format: `dh dashboard schema`.
2. Read what a widget can use: `dh semantic model get <model>` lists its metrics and
   dimensions.
3. Edit the file.
4. `dh --format json dashboard validate <file>`, and fix every error at its line.
5. `dh --format json dashboard preview --once <file>`, and fix every widget whose state is
   `error`. Check that the rows answer what the widget's title says.
6. Never run `dh dashboard apply` without `--dry-run`, and never publish. CI applies the
   files once a person has reviewed them.
```

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
