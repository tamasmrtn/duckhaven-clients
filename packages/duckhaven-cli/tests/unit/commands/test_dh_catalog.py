"""`dh workspace`, `dh catalog`, `dh schema` and `dh table`.

The catalog-resolution and reference-splitting cases carry the weight: with the
workspace-default routes gone from the API, every table URL depends on getting a
catalog from somewhere, and a wrong one reads the wrong data without erroring.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
import respx
from typer.testing import CliRunner

from dh.commands.catalog import split_ref
from dh.errors import ConflictError, ExitCode
from dh.main import app

runner = CliRunner()

HOST = "https://duckhaven.test"
API = f"{HOST}/api"
WS = f"{API}/workspaces/analytics"


def _config(tmp_path, monkeypatch, *, catalog: str | None = None):
    path = tmp_path / "config.toml"
    body = (
        'default_profile = "default"\n\n[profile.default]\n'
        f'host = "{HOST}"\ntoken = "dh_pat_x"\nworkspace = "analytics"\n'
    )
    if catalog:
        body += f'catalog = "{catalog}"\n'
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)
    monkeypatch.setenv("DH_CONFIG_FILE", str(path))
    for var in ("DH_HOST", "DH_TOKEN", "DH_WORKSPACE", "DH_CATALOG", "DH_AGENT", "DH_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return path


@pytest.fixture
def with_catalog(tmp_path, monkeypatch):
    return _config(tmp_path, monkeypatch, catalog="main")


@pytest.fixture
def without_catalog(tmp_path, monkeypatch):
    return _config(tmp_path, monkeypatch)


def _data(result):
    return json.loads(result.stdout)["data"]


# --- Reference splitting ---------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "want", "expected"),
    [
        ("sales.orders", 2, [None, "sales", "orders"]),
        ("main.sales.orders", 2, ["main", "sales", "orders"]),
        ("sales", 1, [None, "sales"]),
        ("main.sales", 1, ["main", "sales"]),
    ],
)
def test_references_split_into_optional_catalog_and_parts(ref, want, expected):
    assert split_ref(ref, want=want) == expected


@pytest.mark.parametrize("ref", ["a.b.c.d", ""])
def test_an_unreadable_reference_says_what_shape_is_wanted(ref):
    with pytest.raises(ConflictError) as exc:
        split_ref(ref, want=2)
    assert "catalog.schema.table" in exc.value.message


# --- Catalog resolution ----------------------------------------------------


@respx.mock
def test_the_profile_catalog_is_used_without_a_lookup(with_catalog):
    route = respx.get(f"{WS}/catalogs/main/schemas/sales/tables").mock(
        return_value=httpx.Response(200, json=[])
    )
    workspace = respx.get(f"{WS}").mock(return_value=httpx.Response(200, json={}))
    assert runner.invoke(app, ["table", "list", "sales"]).exit_code == 0
    assert route.called
    assert not workspace.called


@respx.mock
def test_the_workspace_default_is_fetched_when_nothing_nearer_answers(without_catalog):
    respx.get(f"{WS}").mock(return_value=httpx.Response(200, json={"default_catalog": "wsdefault"}))
    route = respx.get(f"{WS}/catalogs/wsdefault/schemas/sales/tables").mock(
        return_value=httpx.Response(200, json=[])
    )
    assert runner.invoke(app, ["table", "list", "sales"]).exit_code == 0
    assert route.called


@respx.mock
def test_a_workspace_with_no_default_says_how_to_set_one(without_catalog):
    respx.get(f"{WS}").mock(return_value=httpx.Response(200, json={"default_catalog": None}))
    result = runner.invoke(app, ["table", "list", "sales"])
    assert result.exit_code == ExitCode.CONFLICT
    assert "DH_CATALOG" in result.output


@respx.mock
def test_the_catalog_flag_beats_the_profile(with_catalog):
    route = respx.get(f"{WS}/catalogs/other/schemas/sales/tables").mock(
        return_value=httpx.Response(200, json=[])
    )
    assert runner.invoke(app, ["--catalog", "other", "table", "list", "sales"]).exit_code == 0
    assert route.called


@respx.mock
def test_a_three_part_reference_beats_everything(with_catalog):
    """A one-off cross-catalog read stays one command."""
    route = respx.get(f"{WS}/catalogs/archive/schemas/sales/tables/orders").mock(
        return_value=httpx.Response(200, json={"name": "orders"})
    )
    assert runner.invoke(app, ["table", "get", "archive.sales.orders"]).exit_code == 0
    assert route.called


@respx.mock
def test_debug_reports_which_catalog_was_resolved(with_catalog):
    """A wrong default reads the wrong data silently; this is how you find out."""
    respx.get(f"{WS}/catalogs/main/schemas/sales/tables").mock(
        return_value=httpx.Response(200, json=[])
    )
    result = runner.invoke(app, ["--debug", "table", "list", "sales"])
    assert "catalog main" in result.output


# --- Workspaces ------------------------------------------------------------


@respx.mock
def test_workspace_list(with_catalog):
    respx.get(f"{API}/workspaces").mock(
        return_value=httpx.Response(200, json=[{"slug": "analytics"}])
    )
    assert _data(runner.invoke(app, ["--format", "json", "workspace", "list"]))[0]["slug"] == (
        "analytics"
    )


@respx.mock
def test_workspace_create_defaults_the_name_to_the_slug(with_catalog):
    route = respx.post(f"{API}/workspaces").mock(
        return_value=httpx.Response(201, json={"slug": "new"})
    )
    runner.invoke(app, ["workspace", "create", "new"])
    assert json.loads(route.calls[0].request.content) == {"slug": "new", "name": "new"}


@respx.mock
def test_workspace_member_list(with_catalog):
    respx.get(f"{WS}/members").mock(return_value=httpx.Response(200, json=[{"role": "owner"}]))
    assert (
        _data(runner.invoke(app, ["--format", "json", "workspace", "member", "list"]))[0]["role"]
        == "owner"
    )


def test_workspace_update_with_nothing_to_change_is_refused(with_catalog):
    assert runner.invoke(app, ["workspace", "update"]).exit_code == ExitCode.CONFLICT


# --- Catalogs --------------------------------------------------------------


@respx.mock
def test_catalog_list_defaults_to_the_workspace(with_catalog):
    route = respx.get(f"{WS}/catalogs").mock(return_value=httpx.Response(200, json=[]))
    assert runner.invoke(app, ["catalog", "list"]).exit_code == 0
    assert route.called


@respx.mock
def test_catalog_list_all_uses_the_global_route(with_catalog):
    route = respx.get(f"{API}/catalogs").mock(return_value=httpx.Response(200, json=[]))
    assert runner.invoke(app, ["catalog", "list", "--all"]).exit_code == 0
    assert route.called


@respx.mock
def test_catalog_attach_uses_put(with_catalog):
    """Attaching is a membership write, not a verb sub-resource."""
    route = respx.put(f"{WS}/catalogs/shared").mock(return_value=httpx.Response(200, json={}))
    assert runner.invoke(app, ["catalog", "attach", "shared"]).exit_code == 0
    assert route.called


@respx.mock
def test_catalog_detach_uses_delete(with_catalog):
    route = respx.delete(f"{WS}/catalogs/shared").mock(return_value=httpx.Response(204))
    assert runner.invoke(app, ["catalog", "detach", "shared", "--yes"]).exit_code == 0
    assert route.called


# --- Schemas ---------------------------------------------------------------


@respx.mock
def test_schema_list(with_catalog):
    route = respx.get(f"{WS}/catalogs/main/schemas").mock(
        return_value=httpx.Response(200, json=[{"name": "sales"}])
    )
    assert runner.invoke(app, ["schema", "list"]).exit_code == 0
    assert route.called


@respx.mock
def test_schema_create(with_catalog):
    route = respx.post(f"{WS}/catalogs/main/schemas").mock(
        return_value=httpx.Response(201, json={"name": "staging"})
    )
    runner.invoke(app, ["schema", "create", "staging"])
    assert json.loads(route.calls[0].request.content) == {"name": "staging"}


# --- Tables ----------------------------------------------------------------


@respx.mock
def test_table_create_parses_column_specs(with_catalog):
    route = respx.post(f"{WS}/catalogs/main/schemas/sales/tables").mock(
        return_value=httpx.Response(201, json={"name": "orders"})
    )
    runner.invoke(
        app,
        ["table", "create", "sales.orders", "-c", "id:BIGINT:notnull", "-c", "note:VARCHAR"],
    )
    body = json.loads(route.calls[0].request.content)
    assert body["name"] == "orders"
    assert body["columns"] == [
        {"name": "id", "type": "BIGINT", "nullable": False},
        {"name": "note", "type": "VARCHAR", "nullable": True},
    ]


def test_a_malformed_column_spec_says_what_is_wanted(with_catalog):
    result = runner.invoke(app, ["table", "create", "sales.orders", "-c", "id"])
    assert result.exit_code == ExitCode.CONFLICT
    assert "name:type" in result.output


@respx.mock
def test_table_sample_hits_the_sample_route(with_catalog):
    route = respx.get(f"{WS}/catalogs/main/schemas/sales/tables/orders/sample").mock(
        return_value=httpx.Response(200, json={"columns": ["id"], "rows": [{"id": 1}], "total": 1})
    )
    result = runner.invoke(app, ["--format", "json", "table", "sample", "sales.orders"])
    assert result.exit_code == 0
    assert _data(result)["rows"] == [{"id": 1}]
    assert route.called


@respx.mock
@pytest.mark.parametrize("sub", ["snapshots", "lineage", "health"])
def test_the_read_only_table_subresources(with_catalog, sub):
    route = respx.get(f"{WS}/catalogs/main/schemas/sales/tables/orders/{sub}").mock(
        return_value=httpx.Response(200, json=[])
    )
    assert runner.invoke(app, ["table", sub, "sales.orders"]).exit_code == 0
    assert route.called


@respx.mock
def test_table_recount_posts(with_catalog):
    route = respx.post(f"{WS}/catalogs/main/schemas/sales/tables/orders/recount").mock(
        return_value=httpx.Response(200, json={"row_count": 5})
    )
    assert runner.invoke(app, ["table", "recount", "sales.orders"]).exit_code == 0
    assert route.called


@respx.mock
def test_table_drop(with_catalog):
    route = respx.delete(f"{WS}/catalogs/main/schemas/sales/tables/orders").mock(
        return_value=httpx.Response(204)
    )
    assert runner.invoke(app, ["table", "drop", "sales.orders", "--yes"]).exit_code == 0
    assert route.called
