{#
  Snapshots for DuckHaven / Iceberg.

  dbt-duckdb's duckdb__snapshot_merge_sql closes out changed rows with a joined
  `UPDATE <target> SET ... FROM <source> WHERE ...`. duckdb-iceberg documents UPDATE, but
  not the joined UPDATE ... FROM form against an Iceberg table. dbt-core's default uses
  MERGE INTO, which duckdb-iceberg documents as of 1.5.3 — and it collapses two statements
  into one, which also sits better under the session's single-body 600s statement cap.
#}
{% macro duckhaven__snapshot_merge_sql(target, source, insert_cols) -%}
  {{ return(default__snapshot_merge_sql(target, source, insert_cols)) }}
{%- endmacro %}


{#
  Restore dbt-core's default snapshot staging table: a session-local DuckDB TEMP table.

  dbt-duckdb shadows this macro to build a *real* table instead (create_table_as(False, ...)),
  because MotherDuck has no remote temp tables. On DuckHaven that is actively wrong: the
  staging relation's path nulls its database and schema, so a non-temp CREATE renders
  unqualified and lands a real Iceberg table in whatever namespace the session happens to be
  USEing (`analytics`) rather than the snapshot's own schema — real Iceberg churn, in the
  wrong place, on every snapshot run.

  A TEMP table is the same source shape the proven delete+insert incremental path already
  uses (dbt-duckdb's incremental materialization sets temporary=True for non-MotherDuck), so
  it stays local to the agent's session and never touches the catalog. Body is dbt-core's
  build_snapshot_staging_table verbatim.

  This macro is not dispatched, so it shadows by package precedence: dbt resolves internal
  packages in reverse order (dbt, dbt_duckdb, dbt_duckhaven), so ours wins. The duplicate
  name is legal — DuplicateMacroNameError is checked per-package.

  duckdb__post_snapshot still drops the staging relation afterwards, which stays correct.
#}
{% macro build_snapshot_staging_table(strategy, sql, target_relation) %}
    {% set temp_relation = make_temp_relation(target_relation) %}

    {% set select = snapshot_staging_table(strategy, sql, target_relation) %}

    {% call statement('build_snapshot_staging_relation') %}
        {{ create_table_as(True, temp_relation, select) }}
    {% endcall %}

    {% do return(temp_relation) %}
{% endmacro %}
