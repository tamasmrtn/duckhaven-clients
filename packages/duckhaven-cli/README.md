# duckhaven-cli

`dh` — the command-line interface for [DuckHaven](https://github.com/tamasmrtn/duckhaven).

It gives analysts, CI pipelines and operators scriptable access to DuckHaven's REST API:
running SQL, browsing the catalog, publishing dbt lineage and semantic models, managing
grants, and operating service accounts, users and agents.

Like every member of this repo it is a **pure HTTP client of DuckHaven's public REST
API**, authenticating with a Personal Access Token (`dh_pat_…`). The SQL execution path
(sessions, statements, polling, row pagination) is delegated to
[`duckhaven-sql-connector`](../duckhaven-sql-connector/README.md) rather than
reimplemented.

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

## Status

Under construction. The command tree, output contract, error taxonomy and phased build
order are specified in the
[CLI implementation plan](https://github.com/tamasmrtn/duckhaven/blob/main/docs/developer/cli-plan.md),
which is the design record for this package.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
