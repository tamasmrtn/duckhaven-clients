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

## What works

Materializations: **table, seed, incremental, ephemeral**, and **snapshots** (both the
`timestamp` and `check` strategies). Generic and singular tests.

### Incremental strategies

`append`, `delete+insert`, `merge`, and `microbatch`.

`merge` and `microbatch` require the **agent's DuckDB to be 1.5.3 or newer** — that is when
`duckdb-iceberg` gained `MERGE INTO` for Iceberg tables. The adapter checks the agent's
version and simply won't offer the strategies below that, so you get a clear dbt error
instead of a mid-run failure.

`merge` emits the explicit `MERGE INTO … WHEN MATCHED THEN UPDATE SET … WHEN NOT MATCHED
THEN INSERT …` form that duckdb-iceberg documents, rather than dbt-duckdb's DuckDB-native
`UPDATE BY NAME` / `INSERT BY NAME`. `merge_update_columns`, `merge_exclude_columns`, and
`incremental_predicates` work as usual. dbt-duckdb's DuckDB-only extras — `merge_clauses`,
`merge_returning_columns`, `merge_on_using_columns`, `merge_update_condition`,
`merge_insert_condition`, `merge_update_set_expressions` — **raise a compile-time error**
rather than being silently ignored, because they depend on syntax Iceberg does not support.

Because Iceberg writes are merge-on-read, a `merge` target must leave `write.update.mode` /
`write.delete.mode` at `merge-on-read`; DuckDB fails the statement otherwise.

**Microbatch** batches run **serially**, by design: concurrent batches would each take their
own session (an agent admission slot) and race each other to commit to the same Iceberg
table. Size `batch_size` so one batch finishes inside the server's 600s per-statement
limit. Note that each batch leaves a session-local temp table behind for the life of the
run (dbt-duckdb's incremental materialization only cleans those up on MotherDuck), and a
session has a fixed 256 MiB budget — so very high batch counts in one run are untested.

### Not supported (yet)

- **`view` materialization** — DuckDB's Iceberg REST catalog does not implement
  `CREATE VIEW`, so view models fail. Use `table` instead.
- **Python models** — DuckHaven agents execute SQL only; a Python model fails clearly.
- **Re-running `dbt seed` over an existing seed, on an older server** — dbt's seed reset
  emits `TRUNCATE TABLE`. DuckHaven's statement policy admits it, but only since the
  release that added it; against a server predating that, the statement is rejected and
  you need `dbt seed --full-refresh`, which drops and recreates instead. There is no
  capability endpoint to detect this from, so the adapter cannot choose for you.
  Note that on Iceberg `TRUNCATE` is not the cheap metadata-only operation the name
  suggests — it writes positional delete files just as the equivalent `DELETE` would.
- **`on-schema-change` for incremental models** — the ALTER-driven flow is untested here.
  (Snapshots *do* handle a new column: that path is covered.)
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

Cancelling a run (Ctrl-C, or a failure with `--fail-fast`) cancels the in-flight statement
on each session, so the agent's admission slots are freed promptly instead of running the
abandoned queries to completion.

## License

Apache-2.0.
