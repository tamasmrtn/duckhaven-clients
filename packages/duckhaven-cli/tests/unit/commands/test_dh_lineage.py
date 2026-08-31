"""`dh lineage` and `dh semantic` — the dbt publish path.

Two assertions carry the weight: the manifest+catalog envelope is assembled
correctly (that is what unlocks column-level lineage, and it is the part people
get wrong by hand), and the semantic artifact is sent byte for byte rather than
re-encoded.
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

MANIFEST = {"metadata": {"invocation_id": "run-1"}, "nodes": {}}
CATALOG_JSON = {"nodes": {"model.x": {"columns": {}}}}


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(MANIFEST), encoding="utf-8")
    return path


# --- Lineage ---------------------------------------------------------------


@respx.mock
def test_lineage_import_posts_the_manifest_alone(logged_in, manifest):
    route = respx.post(f"{WS}/lineage/imports/dbt").mock(
        return_value=httpx.Response(200, json={"created": 3, "updated": 0, "removed": 0})
    )
    result = runner.invoke(app, ["lineage", "import", "dbt", str(manifest)])
    assert result.exit_code == 0
    assert json.loads(route.calls[0].request.content) == MANIFEST


@respx.mock
def test_catalog_json_is_wrapped_in_the_two_file_envelope(logged_in, manifest, tmp_path):
    """The envelope is what unlocks column-level lineage, and the part people get wrong."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(CATALOG_JSON), encoding="utf-8")
    route = respx.post(f"{WS}/lineage/imports/dbt").mock(
        return_value=httpx.Response(200, json={"created": 1})
    )
    runner.invoke(app, ["lineage", "import", "dbt", str(manifest), "--catalog-json", str(catalog)])
    body = json.loads(route.calls[0].request.content)
    assert body == {"manifest": MANIFEST, "catalog": CATALOG_JSON}


@respx.mock
def test_lineage_import_reports_what_changed(logged_in, manifest):
    respx.post(f"{WS}/lineage/imports/dbt").mock(
        return_value=httpx.Response(
            200, json={"created": 24, "updated": 3, "removed": 1, "skipped": []}
        )
    )
    result = runner.invoke(app, ["lineage", "import", "dbt", str(manifest)])
    assert "created 24, updated 3, removed 1" in result.output


@respx.mock
def test_reconcile_is_passed_through_when_given(logged_in, manifest):
    route = respx.post(f"{WS}/lineage/imports/dbt").mock(return_value=httpx.Response(200, json={}))
    runner.invoke(app, ["lineage", "import", "dbt", str(manifest), "--reconcile", "none"])
    assert route.calls[0].request.url.params["reconcile"] == "none"


@respx.mock
def test_reconcile_is_omitted_so_the_server_default_applies(logged_in, manifest):
    route = respx.post(f"{WS}/lineage/imports/dbt").mock(return_value=httpx.Response(200, json={}))
    runner.invoke(app, ["lineage", "import", "dbt", str(manifest)])
    assert "reconcile" not in route.calls[0].request.url.params


def test_a_malformed_artifact_names_the_file(logged_in, tmp_path):
    broken = tmp_path / "manifest.json"
    broken.write_text("{not json", encoding="utf-8")
    result = runner.invoke(app, ["lineage", "import", "dbt", str(broken)])
    assert result.exit_code == ExitCode.CONFLICT
    assert "manifest.json" in result.output


@respx.mock
def test_import_edges_accepts_a_bare_array(logged_in, tmp_path):
    edges = tmp_path / "edges.json"
    edges.write_text(json.dumps([{"source": {}, "target": {}}]), encoding="utf-8")
    route = respx.post(f"{WS}/lineage/imports").mock(return_value=httpx.Response(200, json={}))
    runner.invoke(app, ["lineage", "import-edges", str(edges), "--provider", "airflow"])
    body = json.loads(route.calls[0].request.content)
    assert body["provider"] == "airflow"
    assert len(body["edges"]) == 1


@respx.mock
def test_lineage_purge_sends_the_provider(logged_in):
    route = respx.delete(f"{WS}/lineage/imports").mock(
        return_value=httpx.Response(200, json={"removed": 7})
    )
    assert runner.invoke(app, ["lineage", "purge", "--provider", "dbt"]).exit_code == 0
    assert route.calls[0].request.url.params["provider"] == "dbt"


# --- Semantic --------------------------------------------------------------


@respx.mock
def test_the_semantic_artifact_is_sent_byte_for_byte(logged_in, tmp_path):
    """The route reads the body raw so a YAML error points at the author's line."""
    doc = tmp_path / "models.yml"
    raw = b"semantic_models:\n  - name: orders\n    # a comment worth keeping\n"
    doc.write_bytes(raw)
    route = respx.post(f"{WS}/semantic/imports/dbt").mock(
        return_value=httpx.Response(200, json={"created": 1})
    )
    result = runner.invoke(app, ["semantic", "import", "dbt", str(doc)])
    assert result.exit_code == 0
    assert route.calls[0].request.content == raw


@respx.mock
def test_semantic_import_says_the_models_are_drafts(logged_in, manifest):
    """Without the promotion step an imported model answers nothing."""
    respx.post(f"{WS}/semantic/imports/dbt").mock(return_value=httpx.Response(200, json={}))
    result = runner.invoke(app, ["semantic", "import", "dbt", str(manifest)])
    assert "drafts" in result.output
    assert "publish" in result.output


@respx.mock
def test_semantic_purge_requires_and_sends_a_provider(logged_in):
    route = respx.delete(f"{WS}/semantic/imports").mock(return_value=httpx.Response(204))
    assert runner.invoke(app, ["semantic", "purge", "--provider", "dbt"]).exit_code == 0
    assert route.calls[0].request.url.params["provider"] == "dbt"


@respx.mock
@pytest.mark.parametrize("action", ["validate", "publish", "deprecate"])
def test_the_model_state_transitions(logged_in, action):
    route = respx.post(f"{WS}/semantic/models/analytics/{action}").mock(
        return_value=httpx.Response(200, json={"state": action})
    )
    assert runner.invoke(app, ["semantic", action, "analytics"]).exit_code == 0
    assert route.called


@respx.mock
def test_semantic_model_list_and_get(logged_in):
    respx.get(f"{WS}/semantic/models").mock(
        return_value=httpx.Response(200, json=[{"slug": "analytics"}])
    )
    respx.get(f"{WS}/semantic/models/analytics").mock(
        return_value=httpx.Response(200, json={"slug": "analytics", "metrics": []})
    )
    assert runner.invoke(app, ["semantic", "model", "list"]).exit_code == 0
    assert runner.invoke(app, ["semantic", "model", "get", "analytics"]).exit_code == 0


# --- Relationships ---------------------------------------------------------


@respx.mock
def test_relationship_add_builds_the_join_columns(logged_in):
    route = respx.post(f"{WS}/semantic/models/analytics/relationships").mock(
        return_value=httpx.Response(201, json={"name": "orders_customer"})
    )
    result = runner.invoke(
        app,
        [
            "semantic",
            "relationship",
            "add",
            "analytics",
            "--name",
            "orders_customer",
            "--left",
            "orders",
            "--right",
            "customers",
            "--join",
            "customer_id=id",
            "--join",
            "tenant=tenant",
        ],
    )
    assert result.exit_code == 0
    body = json.loads(route.calls[0].request.content)
    assert body["left_dataset"] == "orders"
    assert body["right_dataset"] == "customers"
    assert body["join_columns"] == [
        {"left": "customer_id", "right": "id"},
        {"left": "tenant", "right": "tenant"},
    ]


def test_a_malformed_join_says_what_is_wanted(logged_in):
    result = runner.invoke(
        app,
        [
            "semantic",
            "relationship",
            "add",
            "analytics",
            "--name",
            "r",
            "--left",
            "a",
            "--right",
            "b",
            "--join",
            "nonsense",
        ],
    )
    assert result.exit_code == ExitCode.CONFLICT
    assert "left_column=right_column" in result.output


@respx.mock
def test_relationship_remove(logged_in):
    route = respx.delete(f"{WS}/semantic/models/analytics/relationships/r1").mock(
        return_value=httpx.Response(204)
    )
    assert (
        runner.invoke(app, ["semantic", "relationship", "remove", "analytics", "r1"]).exit_code == 0
    )
    assert route.called
