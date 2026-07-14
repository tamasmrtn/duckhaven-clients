{#
  DuckHaven runs DuckDB remotely on an agent, so dbt-duckdb's default seed strategy —
  COPY FROM the client's local CSV — cannot work (the agent has no access to that file,
  and the statement policy blocks COPY from non-staging paths). Load seeds via batched
  INSERT ... VALUES with parameter bindings instead, exactly like dbt-databricks and
  dbt-snowflake. This is dbt's default (non-duckdb) load path.
#}
{% macro duckhaven__load_csv_rows(model, agate_table) %}
  {% set batch_size = get_batch_size() %}
  {% set cols_sql = get_seed_column_quoted_csv(model, agate_table.column_names) %}
  {% set bindings = [] %}
  {% set statements = [] %}

  {% for chunk in agate_table.rows | batch(batch_size) %}
      {% set bindings = [] %}
      {% for row in chunk %}
          {% do bindings.extend(row) %}
      {% endfor %}

      {% set sql %}
          insert into {{ this.render() }} ({{ cols_sql }}) values
          {% for row in chunk -%}
              ({%- for column in agate_table.column_names -%}
                  {{ get_binding_char() }}
                  {%- if not loop.last %},{%- endif %}
              {%- endfor -%})
              {%- if not loop.last %},{%- endif %}
          {%- endfor %}
      {% endset %}

      {% do adapter.add_query(sql, bindings=bindings, abridge_sql_log=True) %}

      {% if loop.index0 == 0 %}
          {% do statements.append(sql) %}
      {% endif %}
  {% endfor %}

  {{ return(statements[0]) }}
{% endmacro %}
