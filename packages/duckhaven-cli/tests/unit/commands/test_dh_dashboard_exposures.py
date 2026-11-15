"""`dh dashboard exposures` — the published dashboards as dbt exposures.

What carries the weight: only published dashboards, each read once with its
semantic models read once between them, become exposures of what they read
in the manifest; one reading nothing there is left out and said to be.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from dh.errors import ExitCode
from dh.main import app

runner = CliRunner()

HOST = "https://duckhaven.test"
WS = f"{HOST}/api/workspaces/analytics"
BOARDS = f"{WS}/dashboards"
MODELS = f"{WS}/semantic/models"

MANIFEST = {
    "metadata": {"project_name": "shop"},
    "nodes": {
        "model.shop.orders": {
            "resource_type": "model",
            "name": "orders",
            "package_name": "shop",
            "database": "lake",
            "schema": "analytics",
            "alias": "fct_orders",
        }
    },
    "sources": {
        "source.shop.raw.events": {
            "source_name": "raw",
            "name": "events",
            "database": "lake",
            "schema": "raw",
            "identifier": "events",
        }
    },
}

SHOP = {
    "slug": "shop",
    "datasets": [
        {
            "name": "orders",
            "catalog": "lake",
            "schema_name": "analytics",
            "table_name": "fct_orders",
        }
    ],
    "metrics": [{"name": "revenue", "dataset": "orders"}, {"name": "orders", "dataset": "orders"}],
    "dimensions": [],
}


def _board(name: str, title: str, *refs: tuple[str, str | None, str], **extra) -> dict:
    return {
        "name": name,
        "title": title,
        "description": None,
        "refs": [{"kind": k, "model": m, "name": n} for k, m, n in refs],
        **extra,
    }


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(MANIFEST), encoding="utf-8")
    return path


def _serve(*boards: dict) -> respx.Route:
    listed = respx.get(BOARDS).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"name": b["name"]} for b in boards],
                "cursor": None,
                "has_more": False,
            },
        )
    )
    for board in boards:
        respx.get(f"{BOARDS}/{board['name']}").mock(return_value=httpx.Response(200, json=board))
    return listed


@respx.mock
def test_exposures_name_what_each_published_dashboard_reads(logged_in, manifest):
    listed = _serve(
        _board(
            "sales-weekly",
            "Sales, weekly",
            ("model", None, "shop"),
            ("metric", "shop", "revenue"),
            ("table", None, "lake.raw.events"),
            description="Revenue by week.",
        ),
        _board("orders", "Orders", ("model", None, "shop"), ("metric", "shop", "orders")),
        _board("notes", "Notes"),
    )
    shop = respx.get(f"{MODELS}/shop").mock(return_value=httpx.Response(200, json=SHOP))

    result = runner.invoke(
        app,
        [
            "dashboard",
            "exposures",
            str(manifest),
            "--owner-name",
            "Data",
            "--owner-email",
            "d@x.io",
        ],
    )

    assert result.exit_code == 0, result.output
    assert listed.calls[0].request.url.params["status"] == "published"
    assert shop.call_count == 1
    assert result.stdout == (
        "# Written by `dh dashboard exposures`. Write it again rather than editing it.\n"
        "version: 2\n"
        "\n"
        "exposures:\n"
        "  - name: sales_weekly\n"
        '    label: "Sales, weekly"\n'
        '    type: "dashboard"\n'
        '    url: "https://duckhaven.test/analytics/dashboards/sales-weekly"\n'
        '    description: "Revenue by week."\n'
        "    owner:\n"
        '      name: "Data"\n'
        '      email: "d@x.io"\n'
        "    depends_on:\n"
        "      - \"ref('orders')\"\n"
        "      - \"source('raw', 'events')\"\n"
        "  - name: orders\n"
        '    label: "Orders"\n'
        '    type: "dashboard"\n'
        '    url: "https://duckhaven.test/analytics/dashboards/orders"\n'
        "    owner:\n"
        '      name: "Data"\n'
        '      email: "d@x.io"\n'
        "    depends_on:\n"
        "      - \"ref('orders')\"\n"
    )
    assert "2 dashboards as exposures." in result.stderr
    assert "Left out, reading nothing in the manifest: notes." in result.stderr


@respx.mock
def test_exposures_say_what_a_dashboard_reads_outside_the_manifest(logged_in, manifest, tmp_path):
    _serve(
        _board(
            "ops",
            "Ops",
            ("table", None, "lake.analytics.fct_orders"),
            ("table", None, "lake.raw.audit"),
        )
    )
    out = tmp_path / "exposures.yml"

    result = runner.invoke(app, ["--output", str(out), "dashboard", "exposures", str(manifest)])

    assert result.exit_code == 0, result.output
    assert "ops also reads lake.raw.audit: not in the manifest." in result.stderr
    text = out.read_text(encoding="utf-8")
    assert '      name: "DuckHaven"\n' in text
    assert "email" not in text
    assert result.stdout == ""


@respx.mock
def test_exposures_skip_a_semantic_model_that_is_gone(logged_in, manifest):
    _serve(_board("old", "Old", ("model", None, "gone"), ("metric", "gone", "revenue")))
    respx.get(f"{MODELS}/gone").mock(
        return_value=httpx.Response(404, json={"error": "not_found", "message": "No model gone."})
    )

    result = runner.invoke(app, ["dashboard", "exposures", str(manifest)])

    assert result.exit_code == 0, result.output
    assert "Semantic model 'gone' is not there to read" in result.stderr
    assert result.stdout.endswith("exposures: []\n")


@respx.mock
def test_exposures_refuse_two_dashboards_with_one_exposure_name(logged_in, manifest):
    tables = ("table", None, "lake.analytics.fct_orders")
    _serve(_board("sales-q1", "Sales", tables), _board("sales_q1", "Sales too", tables))

    result = runner.invoke(app, ["dashboard", "exposures", str(manifest)])

    assert result.exit_code == ExitCode.CONFLICT
    assert "Two dashboards would be the exposure sales_q1" in result.output


def test_exposures_refuse_a_manifest_that_is_not_json(logged_in, tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("not json", encoding="utf-8")

    result = runner.invoke(app, ["dashboard", "exposures", str(path)])

    assert result.exit_code == ExitCode.CONFLICT
    assert "is not valid JSON" in result.output
