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
  Catalog docs (`dbt docs generate`) via the workspace catalog API + DESCRIBE, not
  duckdb_tables()/duckdb_views()/duckdb_columns().

  dbt-duckdb's duckdb__get_catalog joins three DuckDB introspection functions, all
  engine-side enumeration and all rejected outright on a workspace with any scoped catalog
  attached -- the same denial every other override in this file works around. dbt-core
  requires the result back as a real agate.Table (_catalog_filter_table reads
  table.column_names), which a macro built from adapter.list_relation_names alone cannot
  produce without a further SQL round-trip -- so this delegates to a Python adapter method
  that does the REST listing + one DESCRIBE per relation and builds the table directly.
#}
{% macro duckhaven__get_catalog(information_schema, schemas) -%}
  {{ return(adapter.get_catalog_rows(information_schema.database, schemas)) }}
{%- endmacro %}


{#
  dbt-duckdb's duckdb__create_schema probes duckdb_databases() to detect a sqlite-attached
  database before creating the schema (relevant to its sqlite integration, not DuckHaven).
  duckdb_databases() is engine-side enumeration too, and DuckHaven rejects it outright on a
  workspace with any scoped catalog attached. DuckHaven never attaches sqlite, so skip the
  probe and create the schema directly.
#}
{% macro duckhaven__create_schema(relation) -%}
  {%- call statement('create_schema') -%}
    create schema if not exists {{ relation.without_identifier() }}
  {%- endcall -%}
{%- endmacro %}


{#
  Schema listing via the workspace catalog API, not information_schema.schemata.

  dbt-duckdb's duckdb__list_schemas / duckdb__check_schema_exists query
  system.information_schema.schemata, which is rejected outright on a workspace with any
  scoped catalog attached (same class of denial as get_columns_in_relation and
  list_relation_names above). adapter.list_schema_names reads DuckHaven's REST browse
  endpoint instead, which filters by grant.

  dbt-core's own required-schema check (dbt/task/runnable.py) calls this with `database`
  already rendered through the relation's quote policy -- str(relation), not the raw
  `.database` attribute -- so a catalog like `landing` arrives as `"landing"`. The catalog
  API wants the bare slug, so strip the quoting dbt applied before it's forwarded.
#}
{% macro duckhaven__list_schemas(database) -%}
  {% set rows = [] %}
  {%- set clean_database = database.strip('"') if database else database -%}
  {%- for name in adapter.list_schema_names(clean_database) -%}
    {%- do rows.append([name]) -%}
  {%- endfor -%}
  {{ return(rows) }}
{%- endmacro %}

{% macro duckhaven__check_schema_exists(information_schema, schema) -%}
  {% set names = adapter.list_schema_names(information_schema.database) %}
  {% set match_count = 1 if schema | lower in names | map('lower') | list else 0 %}
  {{ return([[match_count]]) }}
{%- endmacro %}


{#
  Relation listing via the workspace catalog API, not information_schema.tables.

  dbt-duckdb's duckdb__list_relations_without_caching queries system.information_schema.
  tables to populate the adapter's relation cache at the start of every run, which is
  rejected outright on a workspace with any scoped catalog attached -- the same denial
  duckhaven__drop_schema already works around for its own listing below, via the same
  adapter.list_relation_names REST call.
#}
{% macro duckhaven__list_relations_without_caching(schema_relation) %}
  {% set rows = [] %}
  {%- for row in adapter.list_relation_names(schema_relation.database, schema_relation.schema) -%}
    {%- set kind = 'view' if row['table_type'] == 'VIEW' else 'table' -%}
    {%- do rows.append([schema_relation.database, row['table_name'], schema_relation.schema, kind]) -%}
  {%- endfor -%}
  {{ return(rows) }}
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
