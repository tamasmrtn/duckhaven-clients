{#
  The `merge` incremental strategy for DuckHaven / Iceberg.

  dbt-duckdb's duckdb__get_merge_sql emits DuckDB-native `WHEN MATCHED THEN UPDATE BY NAME`
  / `WHEN NOT MATCHED THEN INSERT BY NAME`, plus a family of DuckDB-only extras
  (merge_clauses, RETURNING, USING-column joins, per-clause conditions). duckdb-iceberg
  documents MERGE INTO only in its explicit form — UPDATE SET <col> = ... / INSERT (cols)
  VALUES (cols) — and does not document the BY NAME variants against an Iceberg table.
  dbt-core's default__get_merge_sql emits exactly the documented form, so delegate to it.

  NOTE ON THE OVERRIDE TARGET: this overrides get_INCREMENTAL_merge_sql, not get_merge_sql.
  duckdb__get_incremental_merge_sql calls duckdb__get_merge_sql *directly* rather than
  through adapter.dispatch, so a duckhaven__get_merge_sql would never be reached — it would
  be dead code and `merge` would silently keep emitting the BY NAME form.
#}
{% macro duckhaven__get_incremental_merge_sql(args_dict) %}
  {%- do duckhaven__validate_merge_config(config) -%}
  {%- set incremental_predicates = normalize_incremental_predicates(args_dict.get('incremental_predicates')) -%}
  {{ return(default__get_merge_sql(
       args_dict['target_relation'],
       args_dict['temp_relation'],
       args_dict['unique_key'],
       args_dict['dest_columns'],
       incremental_predicates)) }}
{% endmacro %}


{#
  Routing merge through dbt-core's default drops dbt-duckdb's DuckDB-only merge configs.
  Ignoring them silently would be the dangerous failure: `merge_update_condition`, for
  instance, would quietly widen a merge to every matched row. Fail at compile time naming
  what was set, rather than writing wrong data.
#}
{% macro duckhaven__validate_merge_config(config) %}
  {%- set unsupported = [
      'merge_clauses',
      'merge_returning_columns',
      'merge_on_using_columns',
      'merge_update_condition',
      'merge_insert_condition',
      'merge_update_set_expressions',
  ] -%}
  {%- set found = [] -%}
  {%- for name in unsupported -%}
    {%- if config.get(name) -%}
      {%- do found.append(name) -%}
    {%- endif -%}
  {%- endfor -%}
  {%- if found -%}
    {%- do exceptions.raise_compiler_error(
      "dbt-duckhaven does not support these dbt-duckdb merge configs on Iceberg: "
      ~ found | join(', ') ~ ". They rely on DuckDB-native MERGE syntax (BY NAME, RETURNING, "
      ~ "USING-column joins, per-clause conditions) that duckdb-iceberg does not document. "
      ~ "Use merge_update_columns, merge_exclude_columns, or incremental_predicates instead."
    ) -%}
  {%- endif -%}
{% endmacro %}
