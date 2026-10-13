"""`dh worksheet`.

Worksheets are the one resource with optimistic concurrency: the title and the SQL
are saved against a version, so two windows cannot overwrite each other unseen. The
conflict has to reach the user as a conflict, with the current worksheet to hand.
"""

from __future__ import annotations

import json

import httpx
import respx
from typer.testing import CliRunner

from dh.errors import ExitCode
from dh.main import app

runner = CliRunner()

HOST = "https://duckhaven.test"
API = f"{HOST}/api"
WS = f"{API}/workspaces/analytics"
WID = "99999999-9999-9999-9999-999999999999"


def _body(route, index=0):
    return json.loads(route.calls[index].request.content)


@respx.mock
def test_worksheet_list_filters_and_sorts(logged_in):
    route = respx.get(f"{WS}/worksheets").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"id": WID, "title": "Scratch"}], "cursor": None, "has_more": False},
        )
    )
    result = runner.invoke(
        app,
        ["--format", "json", "worksheet", "list", "--status", "open", "--title", "scr"]
        + ["--sort", "title", "--dir", "asc", "--saved-query", "sq1"],
    )
    assert result.exit_code == 0, result.output
    params = route.calls[0].request.url.params
    assert params.get_list("status") == ["open"]
    assert params["q"] == "scr"
    assert params["sort"] == "title"
    assert params["dir"] == "asc"
    assert params["saved_query_id"] == "sq1"
    assert json.loads(result.stdout)["data"][0]["id"] == WID


@respx.mock
def test_worksheet_get(logged_in):
    route = respx.get(f"{WS}/worksheets/{WID}").mock(
        return_value=httpx.Response(200, json={"id": WID, "version": 4})
    )
    result = runner.invoke(app, ["--format", "json", "worksheet", "get", WID])
    assert route.called
    assert json.loads(result.stdout)["data"]["version"] == 4


@respx.mock
def test_worksheet_create_sends_only_what_was_given(logged_in):
    route = respx.post(f"{WS}/worksheets").mock(return_value=httpx.Response(201, json={"id": WID}))
    runner.invoke(
        app,
        ["worksheet", "create", "--title", "Scratch", "-q", "select 1"]
        + ["--catalog", "sales", "--timeout", "5m", "--closed"],
    )
    assert _body(route) == {
        "title": "Scratch",
        "sql": "select 1",
        "catalog": "sales",
        "timeout_s": 300.0,
        "is_open": False,
    }


@respx.mock
def test_worksheet_create_with_nothing_is_an_empty_tab(logged_in):
    route = respx.post(f"{WS}/worksheets").mock(return_value=httpx.Response(201, json={"id": WID}))
    runner.invoke(app, ["worksheet", "create"])
    assert _body(route) == {}


@respx.mock
def test_saving_sql_sends_the_version_it_was_read_at(logged_in):
    route = respx.patch(f"{WS}/worksheets/{WID}").mock(
        return_value=httpx.Response(200, json={"id": WID, "version": 5})
    )
    result = runner.invoke(
        app, ["worksheet", "update", WID, "-q", "select 2", "--base-version", "4"]
    )
    assert result.exit_code == 0, result.output
    assert _body(route) == {"sql": "select 2", "base_version": 4}


@respx.mock
def test_changing_content_without_a_version_is_refused_before_asking(logged_in):
    route = respx.patch(f"{WS}/worksheets/{WID}")
    for argv in (["-q", "select 2"], ["--title", "New"]):
        result = runner.invoke(app, ["worksheet", "update", WID, *argv])
        assert result.exit_code == ExitCode.CONFLICT
        assert "--base-version" in result.stderr
    assert not route.called


@respx.mock
def test_metadata_needs_no_version(logged_in):
    """Agent, catalog, timeout and open/closed are last write wins on the server."""
    route = respx.patch(f"{WS}/worksheets/{WID}").mock(
        return_value=httpx.Response(200, json={"id": WID})
    )
    runner.invoke(app, ["worksheet", "update", WID, "--catalog", "raw", "--closed"])
    assert _body(route) == {"catalog": "raw", "is_open": False}


def test_update_with_nothing_to_change_is_refused(logged_in):
    result = runner.invoke(app, ["worksheet", "update", WID])
    assert result.exit_code == ExitCode.CONFLICT


@respx.mock
def test_a_concurrent_edit_is_a_conflict_that_carries_the_current_worksheet(logged_in):
    current = {"id": WID, "version": 7, "sql": "select 99"}
    respx.patch(f"{WS}/worksheets/{WID}").mock(
        return_value=httpx.Response(
            409,
            json={
                "error": "worksheet_conflict",
                "message": "The worksheet changed since version 4.",
                "details": {"current": current},
            },
        )
    )
    result = runner.invoke(
        app,
        ["--format", "json", "worksheet", "update", WID, "-q", "select 2", "--base-version", "4"],
    )
    assert result.exit_code == ExitCode.CONFLICT
    assert "changed since" in result.stderr
    assert '"version": 7' in result.stderr


@respx.mock
def test_worksheet_delete_asks_first(logged_in):
    route = respx.delete(f"{WS}/worksheets/{WID}").mock(return_value=httpx.Response(204))
    assert runner.invoke(app, ["worksheet", "delete", WID]).exit_code != 0
    assert not route.called
    assert runner.invoke(app, ["worksheet", "delete", WID, "--yes"]).exit_code == 0
    assert route.called
