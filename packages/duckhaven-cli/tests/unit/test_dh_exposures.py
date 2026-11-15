"""dbt exposures from dashboards: matching DuckHaven tables to dbt relations, and the file.

The matching is the part worth pinning: a table is a relation when catalog,
schema and name agree with database, schema and alias, as the server's dbt
import matches them, and a dashboard's semantic reads come down to the
datasets of the metrics and dimensions it names.
"""

from __future__ import annotations

import pytest

from dh import exposures

MANIFEST = {
    "metadata": {"project_name": "shop"},
    "nodes": {
        "model.shop.orders": {
            "resource_type": "model",
            "name": "orders",
            "package_name": "shop",
            "database": "Lake",
            "schema": "analytics",
            "alias": "fct_orders",
        },
        "model.shop.customers": {
            "resource_type": "model",
            "name": "customers",
            "package_name": "shop",
            "database": "lake",
            "schema": "analytics",
            "alias": None,
        },
        "seed.shop.countries": {
            "resource_type": "seed",
            "name": "countries",
            "package_name": "shop",
            "database": "lake",
            "schema": "seeds",
        },
        "snapshot.shop.prices": {
            "resource_type": "snapshot",
            "name": "prices",
            "package_name": "shop",
            "database": "lake",
            "schema": "snapshots",
            "alias": "prices",
        },
        "model.utils.calendar": {
            "resource_type": "model",
            "name": "calendar",
            "package_name": "utils",
            "database": "lake",
            "schema": "utils",
            "alias": "calendar",
        },
        "model.shop.dim_users.v2": {
            "resource_type": "model",
            "name": "dim_users",
            "package_name": "shop",
            "version": 2,
            "database": "lake",
            "schema": "analytics",
            "alias": "dim_users_v2",
        },
        "test.shop.not_null": {
            "resource_type": "test",
            "name": "not_null",
            "database": "lake",
            "schema": "analytics",
        },
        "model.shop.nowhere": {"resource_type": "model", "name": "nowhere", "schema": "x"},
    },
    "sources": {
        "source.shop.raw.events": {
            "source_name": "raw",
            "name": "events",
            "database": "lake",
            "schema": "raw",
            "identifier": "events_v1",
        },
        "source.shop.raw.nowhere": {"source_name": "raw", "name": "nowhere", "schema": "raw"},
        "source.shop.raw.users": {
            "source_name": "raw",
            "name": "users",
            "database": "lake",
            "schema": "raw",
            "identifier": None,
        },
    },
}


def test_relations_name_what_the_manifest_builds_or_declares():
    assert exposures.relations(MANIFEST) == {
        ("lake", "analytics", "fct_orders"): "ref('orders')",
        ("lake", "analytics", "customers"): "ref('customers')",
        ("lake", "seeds", "countries"): "ref('countries')",
        ("lake", "snapshots", "prices"): "ref('prices')",
        ("lake", "utils", "calendar"): "ref('utils', 'calendar')",
        ("lake", "analytics", "dim_users_v2"): "ref('dim_users', v=2)",
        ("lake", "raw", "events_v1"): "source('raw', 'events')",
        ("lake", "raw", "users"): "source('raw', 'users')",
    }


def test_relations_of_an_empty_manifest():
    assert exposures.relations({}) == {}


ORDERS = {
    "slug": "orders",
    "datasets": [
        {
            "name": "orders",
            "catalog": "lake",
            "schema_name": "analytics",
            "table_name": "fct_orders",
        },
        {
            "name": "customers",
            "catalog": "lake",
            "schema_name": "analytics",
            "table_name": "customers",
        },
        {"name": "events", "catalog": None, "schema_name": "raw", "table_name": "events_v1"},
    ],
    "metrics": [
        {"name": "revenue", "dataset": "orders"},
        {"name": "visits", "dataset": "events"},
        {"name": "legacy", "dataset": None},
        {"name": "orphan", "dataset": "dropped"},
    ],
    "dimensions": [{"name": "country", "dataset": "customers"}],
}


def _board(*refs: tuple[str, str | None, str]) -> dict:
    return {"refs": [{"kind": k, "model": m, "name": n} for k, m, n in refs]}


def _models(slug: str) -> dict | None:
    return ORDERS if slug == "orders" else None


def test_tables_are_the_datasets_of_the_members_a_dashboard_reads():
    board = _board(
        ("model", None, "orders"),
        ("metric", "orders", "revenue"),
        ("dimension", "orders", "country"),
        ("table", None, "lake.raw.events_v1"),
    )
    assert exposures.tables(board, _models) == {
        "lake.analytics.fct_orders",
        "lake.analytics.customers",
        "lake.raw.events_v1",
    }


def test_a_dataset_without_a_catalog_is_named_without_one():
    board = _board(("model", None, "orders"), ("metric", "orders", "visits"))
    assert exposures.tables(board, _models) == {"raw.events_v1"}


@pytest.mark.parametrize(
    "refs",
    [
        [("model", None, "orders")],
        [("model", None, "orders"), ("metric", "orders", "legacy")],
    ],
    ids=["model-only", "member-without-dataset"],
)
def test_reading_a_model_without_saying_where_reads_every_dataset(refs):
    assert len(exposures.tables(_board(*refs), _models)) == 3


@pytest.mark.parametrize("metric", ["removed", "orphan"])
def test_a_metric_gone_from_its_model_or_its_dataset_reads_nothing(metric):
    board = _board(("model", None, "orders"), ("metric", "orders", metric))
    assert exposures.tables(board, _models) == set()


def test_a_model_that_cannot_be_read_is_skipped():
    board = _board(("model", None, "gone"), ("metric", "gone", "revenue"))
    assert exposures.tables(board, _models) == set()


def test_depends_on_matches_by_catalog_schema_and_table_ignoring_case():
    known = exposures.relations(MANIFEST)
    entries, missing = exposures.depends_on(
        {"LAKE.analytics.FCT_ORDERS", "lake.raw.events_v1", "lake.other.table"}, known
    )
    assert entries == ["ref('orders')", "source('raw', 'events')"]
    assert missing == ["lake.other.table"]


def test_depends_on_matches_a_name_without_its_catalog_only_when_one_relation_has_it():
    known = exposures.relations(MANIFEST)
    known[("other", "seeds", "countries")] = "source('other', 'countries')"
    entries, missing = exposures.depends_on(
        {"analytics.fct_orders", "seeds.countries", "fct_orders"}, known
    )
    assert entries == ["ref('orders')"]
    assert missing == ["fct_orders", "seeds.countries"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [("sales-weekly", "sales_weekly"), ("Sales.Q1", "sales_q1"), ("ops_2", "ops_2")],
)
def test_an_exposure_takes_letters_digits_and_underscores(name, expected):
    assert exposures.name_of(name) == expected


def test_render_writes_a_dbt_properties_file():
    text = exposures.render(
        [
            {
                "name": "sales_weekly",
                "label": 'Sales: "weekly" — €',
                "type": "dashboard",
                "url": "https://dh.test/analytics/dashboards/sales-weekly",
                "description": "Revenue.\nBy week.",
                "owner": {"name": "Data team", "email": None},
                "depends_on": ["ref('orders')", "source('raw', 'events')"],
            }
        ]
    )
    assert text == (
        "# Written by `dh dashboard exposures`. Write it again rather than editing it.\n"
        "version: 2\n"
        "\n"
        "exposures:\n"
        "  - name: sales_weekly\n"
        '    label: "Sales: \\"weekly\\" — €"\n'
        '    type: "dashboard"\n'
        '    url: "https://dh.test/analytics/dashboards/sales-weekly"\n'
        '    description: "Revenue.\\nBy week."\n'
        "    owner:\n"
        '      name: "Data team"\n'
        "    depends_on:\n"
        "      - \"ref('orders')\"\n"
        "      - \"source('raw', 'events')\"\n"
    )
    yaml = pytest.importorskip("yaml")
    [read] = yaml.safe_load(text)["exposures"]
    assert read["label"] == 'Sales: "weekly" — €'
    assert read["description"] == "Revenue.\nBy week."
    assert read["depends_on"] == ["ref('orders')", "source('raw', 'events')"]


def test_render_with_nothing_to_expose():
    assert exposures.render([]).endswith("version: 2\n\nexposures: []\n")
