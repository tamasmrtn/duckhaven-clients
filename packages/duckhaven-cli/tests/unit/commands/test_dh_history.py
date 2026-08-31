"""`dh query list`, `dh saved-query`, `dh schedule` and `dh search`."""

from __future__ import annotations

import json
import os

import httpx
import pytest
import respx
from typer.testing import CliRunner

from dh.errors import ExitCode
from dh.main import app

runner = CliRunner()

HOST = "https://duckhaven.test"
API = f"{HOST}/api"
WS = f"{API}/workspaces/analytics"
SQ = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def logged_in(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        'default_profile = "default"\n\n[profile.default]\n'
        f'host = "{HOST}"\ntoken = "dh_pat_x"\nworkspace = "analytics"\n',
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    monkeypatch.setenv("DH_CONFIG_FILE", str(path))
    for var in ("DH_HOST", "DH_TOKEN", "DH_WORKSPACE", "DH_CATALOG", "DH_AGENT", "DH_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return path


def _data(result):
    return json.loads(result.stdout)["data"]


# --- dh query list ---------------------------------------------------------


@respx.mock
def test_query_list_returns_the_page_envelope(logged_in):
    respx.get(f"{WS}/queries").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "1"}], "cursor": "c1", "has_more": True}
        )
    )
    result = runner.invoke(app, ["--format", "json", "query", "list"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert body["data"] == [{"id": "1"}]
    assert body["cursor"] == "c1"
    assert body["has_more"] is True


@respx.mock
def test_query_list_sends_every_filter_it_offers(logged_in):
    route = respx.get(f"{WS}/queries").mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None, "has_more": False})
    )
    runner.invoke(
        app,
        [
            "query",
            "list",
            "--status",
            "failed",
            "--status",
            "cancelled",
            "--statement-type",
            "select",
            "--since",
            "2026-01-01T00:00:00Z",
            "--until",
            "2026-02-01T00:00:00Z",
            "--origin",
            "session",
            "--agent",
            "a1",
            "--user",
            "u1",
            "-q",
            "orders",
            "--slower-than",
            "500",
            "--sort",
            "duration",
            "--dir",
            "asc",
            "--limit",
            "10",
        ],
    )
    params = route.calls[0].request.url.params
    assert params.get_list("status") == ["failed", "cancelled"]
    assert params["statement_type"] == "select"
    assert params["origin"] == "session"
    assert params["q"] == "orders"
    assert params["slower_than_ms"] == "500"
    assert params["sort"] == "duration"
    assert params["dir"] == "asc"
    assert params["limit"] == "10"


@respx.mock
def test_query_list_omits_filters_that_were_not_given(logged_in):
    """`status` has no server-side default; sending it empty narrows to nothing."""
    route = respx.get(f"{WS}/queries").mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None, "has_more": False})
    )
    runner.invoke(app, ["query", "list"])
    params = route.calls[0].request.url.params
    for absent in ("status", "origin", "q", "sort", "all_workspaces"):
        assert absent not in params


@respx.mock
def test_query_list_all_walks_every_page(logged_in):
    route = respx.get(f"{WS}/queries").mock(
        side_effect=[
            httpx.Response(200, json={"items": [{"id": "1"}], "cursor": "c1", "has_more": True}),
            httpx.Response(200, json={"items": [{"id": "2"}], "cursor": None, "has_more": False}),
        ]
    )
    result = runner.invoke(app, ["--format", "json", "query", "list", "--all"])
    assert [row["id"] for row in _data(result)] == ["1", "2"]
    assert route.call_count == 2


# --- dh saved-query --------------------------------------------------------


@respx.mock
def test_saved_query_list(logged_in):
    respx.get(f"{WS}/saved-queries").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": SQ, "name": "report"}], "cursor": None, "has_more": False}
        )
    )
    assert _data(runner.invoke(app, ["--format", "json", "saved-query", "list"]))[0]["name"] == (
        "report"
    )


@respx.mock
def test_saved_query_create_from_a_flag(logged_in):
    route = respx.post(f"{WS}/saved-queries").mock(
        return_value=httpx.Response(201, json={"id": SQ, "name": "report"})
    )
    result = runner.invoke(app, ["saved-query", "create", "report", "-q", "select 1"])
    assert result.exit_code == 0
    assert json.loads(route.calls[0].request.content) == {"name": "report", "sql": "select 1"}


@respx.mock
def test_saved_query_create_from_a_file(logged_in, tmp_path):
    script = tmp_path / "r.sql"
    script.write_text("select 2\n", encoding="utf-8")
    route = respx.post(f"{WS}/saved-queries").mock(
        return_value=httpx.Response(201, json={"id": SQ})
    )
    runner.invoke(app, ["saved-query", "create", "report", "-f", str(script)])
    assert json.loads(route.calls[0].request.content)["sql"] == "select 2\n"


def test_saved_query_create_needs_exactly_one_source(logged_in):
    result = runner.invoke(app, ["saved-query", "create", "report"])
    assert result.exit_code == ExitCode.CONFLICT


@respx.mock
def test_saved_query_update_sends_only_what_changed(logged_in):
    route = respx.patch(f"{WS}/saved-queries/{SQ}").mock(
        return_value=httpx.Response(200, json={"id": SQ})
    )
    runner.invoke(app, ["saved-query", "update", SQ, "--name", "renamed"])
    assert json.loads(route.calls[0].request.content) == {"name": "renamed"}


def test_saved_query_update_with_nothing_to_change_is_refused(logged_in):
    result = runner.invoke(app, ["saved-query", "update", SQ])
    assert result.exit_code == ExitCode.CONFLICT
    assert "--name" in result.output


@respx.mock
def test_saved_query_delete(logged_in):
    route = respx.delete(f"{WS}/saved-queries/{SQ}").mock(return_value=httpx.Response(204))
    assert runner.invoke(app, ["saved-query", "delete", SQ]).exit_code == 0
    assert route.called


# --- dh schedule -----------------------------------------------------------


@respx.mock
def test_schedule_create_defaults_to_enabled(logged_in):
    route = respx.post(f"{WS}/schedules").mock(return_value=httpx.Response(201, json={"id": "s1"}))
    runner.invoke(app, ["schedule", "create", SQ, "--cron", "0 6 * * *"])
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "job_type": "saved_query",
        "saved_query_id": SQ,
        "cron": "0 6 * * *",
        "enabled": True,
    }


@respx.mock
def test_schedule_create_can_start_paused(logged_in):
    route = respx.post(f"{WS}/schedules").mock(return_value=httpx.Response(201, json={"id": "s1"}))
    runner.invoke(app, ["schedule", "create", SQ, "--cron", "0 6 * * *", "--disabled"])
    assert json.loads(route.calls[0].request.content)["enabled"] is False


@respx.mock
def test_schedule_update_can_disable_without_touching_the_cron(logged_in):
    route = respx.patch(f"{WS}/schedules/s1").mock(return_value=httpx.Response(200, json={}))
    runner.invoke(app, ["schedule", "update", "s1", "--disabled"])
    assert json.loads(route.calls[0].request.content) == {"enabled": False}


@respx.mock
def test_schedule_runs_for_one_schedule(logged_in):
    route = respx.get(f"{WS}/schedules/s1/runs").mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None, "has_more": False})
    )
    assert runner.invoke(app, ["schedule", "runs", "s1"]).exit_code == 0
    assert route.called


@respx.mock
def test_schedule_runs_without_an_id_uses_the_workspace_feed(logged_in):
    route = respx.get(f"{WS}/schedule-runs").mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None, "has_more": False})
    )
    assert runner.invoke(app, ["schedule", "runs"]).exit_code == 0
    assert route.called


# --- dh search -------------------------------------------------------------


@respx.mock
def test_search_reports_has_more_without_a_cursor(logged_in):
    """Search is a truncated report, not a page; there is nothing to walk."""
    respx.get(f"{WS}/search").mock(
        return_value=httpx.Response(200, json={"items": [{"name": "orders"}], "has_more": True})
    )
    body = json.loads(runner.invoke(app, ["--format", "json", "search", "ord"]).stdout)
    assert body["data"] == [{"name": "orders"}]
    assert body["has_more"] is True
    assert body["cursor"] is None


@respx.mock
def test_search_sends_the_term(logged_in):
    route = respx.get(f"{WS}/search").mock(
        return_value=httpx.Response(200, json={"items": [], "has_more": False})
    )
    runner.invoke(app, ["search", "orders", "--limit", "5"])
    assert route.calls[0].request.url.params["q"] == "orders"
    assert route.calls[0].request.url.params["limit"] == "5"
