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
