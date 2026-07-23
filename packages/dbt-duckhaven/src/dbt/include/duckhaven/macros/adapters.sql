{#
  Column introspection via DESCRIBE.

  dbt-duckdb's duckdb__get_columns_in_relation queries system.information_schema.columns.
  That is broken two ways on DuckHaven: it cannot introspect an attached Iceberg REST
  table (returning a single '__'/UNKNOWN placeholder row, and inconsistently so -- a table
  something has already touched in the session reports correctly while the rest do not),
  and it is rejected outright on a workspace with a scoped catalog attached. DESCRIBE is
  DuckHaven's stated contract for columns; it is grant-checked per relation rather than
  denied. Reshape it into the (name, data_type, char_max_length, numeric_precision,
  numeric_scale) tuples that sql_convert_columns_in_relation consumes positionally.
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
  schema's relations individually, then drop the now-empty schema without CASCADE.

  The listing comes from adapter.list_relation_names, not information_schema.tables:
  engine-side enumeration is rejected outright on any workspace with a scoped catalog
  attached (DuckDB computes those listings across every attachment and cannot filter them
  by grant), which made this macro fail on every drop_schema there. The adapter method
  reads DuckHaven's REST browse endpoint, which filters by grant.
#}
{% macro duckhaven__drop_schema(relation) -%}
  {%- if execute -%}
    {%- for row in adapter.list_relation_names(relation.database, relation.schema) -%}
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


{#
  DuckDB's Iceberg REST catalog rejects DROP ... CASCADE (same limitation as drop_schema).
  dbt-duckdb's duckdb__drop_relation appends CASCADE for non-DuckLake relations, which the
  table materialization hits on every rebuild (it drops the renamed backup relation). Drop
  without CASCADE.
#}
{% macro duckhaven__drop_relation(relation) -%}
  {%- call statement('drop_relation', auto_begin=False) -%}
    drop {{ relation.type }} if exists {{ relation }}
  {%- endcall -%}
{%- endmacro %}
