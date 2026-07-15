# dbt-duckhaven

A [dbt](https://www.getdbt.com/) adapter that runs your DuckDB models on **DuckHaven**
compute through the DuckHaven API. Instead of dbt
opening its own in-process DuckDB, every statement is routed through the DuckHaven
session API, which dispatches it to an agent. dbt authenticates as a DuckHaven
service-account personal access token (PAT), and identifies itself to the server via the
connector's User-Agent (`dbt-duckhaven/<version>`), so a `dbt run` is fully governed and
attributable.

It reuses [`dbt-duckdb`](https://github.com/duckdb/dbt-duckdb) wholesale — the DuckDB
dialect, the macros, and the Iceberg-aware materializations — and swaps **only** the
connection layer for the [`duckhaven-sql-connector`](../duckhaven-sql-connector) session
client.

## Install

```bash
pip install dbt-duckhaven
```

## Configure (`profiles.yml`)

```yaml
my_project:
  target: dev
  outputs:
    dev:
      type: duckhaven
      host: https://duckhaven.internal        # DuckHaven API base URL
      workspace: analytics                     # DuckHaven workspace slug
      token: "{{ env_var('DUCKHAVEN_PAT') }}"  # a DuckHaven PAT (dh_pat_…)
      agent: 00000000-0000-0000-0000-000000000000  # optional agent UUID; omit to auto-pick
      catalog: sales                           # required — dbt "database" → Polaris catalog
      schema: analytics                        # dbt "schema"   → Polaris namespace
      threads: 4
```

`host`, `workspace`, `token`, and `catalog` are required; `agent` is optional (omit to let
the API pick a compatible connected agent). `dbt init` scaffolds these prompts for you.

## What works in v1

Materializations: **table, seed, incremental** (`append` and `delete+insert` strategies),
and **ephemeral**. Generic and singular tests.

### Not supported (yet)

- **`view` materialization** — DuckDB's Iceberg REST catalog does not implement
  `CREATE VIEW`, so view models fail. Use `table` instead.
- **Python models** — DuckHaven agents execute SQL only; a Python model fails clearly.
- **Snapshots** and the **`merge` incremental strategy** — deferred (need Iceberg MERGE
  / temp-relation semantics that are still stabilizing).
- **`external` materialization / dbt-duckdb source plugins** — out of scope.

### Model constraints

Polaris/Iceberg does not enforce relational constraints. In model contracts, `not_null` is
enforced (rejected on write), while `primary_key`, `foreign_key`, and `unique` are
**documentation-only** (not enforced), and `check` constraints are not supported. The
adapter advertises this to dbt so contracts don't promise enforcement the backend can't
provide.

> **Server requirement.** The adapter needs a DuckHaven build whose SQL session/statement
> API is enabled and whose statement policy admits `DESCRIBE` (used for column metadata).

## Concurrency

`threads: N` opens **N** DuckHaven sessions, each holding one agent admission slot for
the duration of the run. There is no deadlock, but keep `threads ≤` the agent's
admission capacity to avoid queueing/starvation. `threads: 2–4` is a good default.

## License

Apache-2.0.
