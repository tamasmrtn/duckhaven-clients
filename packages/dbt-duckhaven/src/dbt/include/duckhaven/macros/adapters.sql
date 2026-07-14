{#
  Column introspection via DESCRIBE.

  dbt-duckdb's duckdb__get_columns_in_relation queries system.information_schema.columns,
  which is known-broken for attached Iceberg REST catalogs on DuckHaven. DESCRIBE returns
  correct column names and types for the same relations, so we reshape it into the
  (name, data_type, char_max_length, numeric_precision, numeric_scale) tuples that
  sql_convert_columns_in_relation consumes positionally.
#}
{% macro duckhaven__get_columns_in_relation(relation) -%}
  {% call statement('get_columns_in_relation', fetch_result=True) %}
      select
          column_name,
          column_type as data_type,
          null as character_maximum_length,
          null as numeric_precision,
          null as numeric_scale
      from (describe {{ relation }})
  {% endcall %}
  {% set table = load_result('get_columns_in_relation').table %}
  {{ return(sql_convert_columns_in_relation(table)) }}
{% endmacro %}


{#
  DuckDB's Iceberg REST catalog does not support DROP SCHEMA ... CASCADE, so we drop the
  schema's relations individually (information_schema.tables works across Iceberg
  catalogs), then drop the now-empty schema without CASCADE.
#}
{% macro duckhaven__drop_schema(relation) -%}
  {%- if execute -%}
    {%- set list_sql -%}
      select table_name, table_type
      from information_schema.tables
      where table_catalog = '{{ relation.database }}'
        and table_schema = '{{ relation.schema }}'
    {%- endset -%}
    {%- for row in run_query(list_sql) -%}
      {%- set kind = 'view' if row['table_type'] == 'VIEW' else 'table' -%}
      {%- call statement('drop_' ~ loop.index) -%}
        drop {{ kind }} if exists {{ adapter.quote(relation.database) }}.{{ adapter.quote(relation.schema) }}.{{ adapter.quote(row['table_name']) }}
      {%- endcall -%}
    {%- endfor -%}
  {%- endif -%}
  {%- call statement('drop_schema') -%}
    drop schema if exists {{ relation.without_identifier() }}
  {%- endcall -%}
{%- endmacro %}
