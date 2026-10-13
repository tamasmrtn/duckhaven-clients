# duckhaven-cli

`dh` — the command-line interface for [DuckHaven](https://github.com/tamasmrtn/duckhaven).

It gives analysts, CI pipelines and operators scriptable access to DuckHaven's REST API:
running SQL, browsing the catalog, publishing dbt lineage and semantic models, managing
grants, and operating service accounts, users and agents.

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

## Documentation

- [Command-line quickstart](https://tamasmrtn.github.io/duckhaven/getting-started/cli-quickstart/) — install, sign in,
  run a query
- [CLI reference](https://tamasmrtn.github.io/duckhaven/reference/cli/) — every command and flag

## Licence

Apache-2.0. See [LICENSE](LICENSE).
