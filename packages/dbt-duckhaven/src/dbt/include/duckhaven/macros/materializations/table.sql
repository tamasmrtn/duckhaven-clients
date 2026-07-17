{#
  Table materialization for DuckHaven / Iceberg.

  dbt-duckdb's table materialization builds a `<name>__dbt_tmp` relation and then
  `ALTER ... RENAME TO` it into place. On an Iceberg REST catalog a rename moves the
  catalog entry but NOT the storage location, so the renamed table keeps living at the
  `__dbt_tmp` path — and the next run's `CREATE ... __dbt_tmp` fails with a Polaris
  location conflict (403). Build the table directly at its own location instead: drop any
  existing relation (without CASCADE — see duckhaven__drop_relation), then CREATE TABLE AS.
  No intermediate relation, no rename.

  The DuckHaven session is autocommit (disable_transactions), so there is no atomic
  backup/rename swap to preserve — a per-statement commit already lands each step.
#}
{% materialization table, adapter='duckhaven', supported_languages=['sql'] %}

  {%- set existing_relation = load_cached_relation(this) -%}
  {%- set target_relation = this.incorporate(type='table') -%}
  {%- set grant_config = config.get('grants') -%}

  {{ run_hooks(pre_hooks) }}

  {%- if existing_relation is not none %}
    {{ adapter.drop_relation(existing_relation) }}
  {%- endif %}

  {% call statement('main') -%}
    {{ create_table_as(False, target_relation, compiled_code) }}
  {%- endcall %}

  {{ run_hooks(post_hooks) }}

  {% set should_revoke = should_revoke(existing_relation, full_refresh_mode=True) %}
  {% do apply_grants(target_relation, grant_config, should_revoke=should_revoke) %}

  {% do persist_docs(target_relation, model) %}

  {{ return({'relations': [target_relation]}) }}

{% endmaterialization %}
