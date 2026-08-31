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

## Documentation

- [Command-line quickstart](https://tamasmrtn.github.io/duckhaven/getting-started/cli-quickstart/) — install, sign in,
  run a query
- [CLI reference](https://tamasmrtn.github.io/duckhaven/reference/cli/) — every command and flag

## Licence

Apache-2.0. See [LICENSE](LICENSE).
