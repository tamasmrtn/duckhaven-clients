"""`dh sql` and `dh query`.

Two cases here are the ones the design called out by name: multi-page results must
be fetched to the end, and Ctrl-C must cancel the query server-side rather than
orphan it on an agent.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
import respx
from typer.testing import CliRunner

from dh import execute
from dh.errors import ConflictError, ExitCode
from dh.main import app
from dh.rest import RestClient

runner = CliRunner()

HOST = "https://duckhaven.test"
API = f"{HOST}/api"
QID = "11111111-2222-3333-4444-555555555555"
AGENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def logged_in(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        'default_profile = "default"\n\n[profile.default]\n'
        f'host = "{HOST}"\ntoken = "dh_pat_x"\nworkspace = "analytics"\nagent = "{AGENT}"\n',
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    monkeypatch.setenv("DH_CONFIG_FILE", str(path))
    for var in ("DH_HOST", "DH_TOKEN", "DH_WORKSPACE", "DH_CATALOG", "DH_AGENT", "DH_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return path


@pytest.fixture
def client():
    with RestClient(HOST, "dh_pat_x") as rest:
        yield rest


def _query(status="done", **extra):
    return {"id": QID, "status": status, "row_count": 1, "error": None, **extra}


def _rows_page(rows, cursor, total):
    """Rows as the server sends them: dicts keyed by column name, not positional."""
    return {
        "columns": ["n"],
        "rows": [{"n": v} for v in rows],
        "cursor": cursor,
        "total": total,
    }


def _mock_run(statuses=("done",), rows=None, total=None):
    respx.post(f"{API}/workspaces/analytics/queries").mock(
        return_value=httpx.Response(202, json=_query("queued"))
    )
    respx.get(f"{API}/queries/{QID}").mock(
        side_effect=[httpx.Response(200, json=_query(s)) for s in statuses]
    )
    body = rows if rows is not None else [[1]]
    respx.get(f"{API}/queries/{QID}/rows").mock(
        return_value=httpx.Response(200, json=_rows_page(body, None, total or len(body)))
    )


# --- Duration parsing ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("30", 30), ("30s", 30), ("5m", 300), ("1h", 3600), ("1.5m", 90), (" 20m ", 1200)],
)
def test_durations_accept_the_shapes_people_type(text, seconds):
    assert execute.parse_duration(text) == seconds


def test_an_unreadable_duration_says_what_is_accepted():
    with pytest.raises(ConflictError) as exc:
        execute.parse_duration("soon")
    assert "30s" in exc.value.message


# --- Row pagination --------------------------------------------------------


@respx.mock
def test_every_page_of_results_is_fetched(client):
    """The named truncation trap: one fetch returns page one and looks complete."""
    route = respx.get(f"{API}/queries/{QID}/rows").mock(
        side_effect=[
            httpx.Response(200, json=_rows_page([1, 2], "2", 5)),
            httpx.Response(200, json=_rows_page([3, 4], "4", 5)),
            httpx.Response(200, json=_rows_page([5], None, 5)),
        ]
    )
    page = execute.fetch_rows(client, QID)
    assert page["rows"] == [{"n": v} for v in (1, 2, 3, 4, 5)]
    assert route.call_count == 3
    assert route.calls[1].request.url.params["cursor"] == "2"


@respx.mock
def test_limit_stops_fetching_and_reports_truncation(client):
    route = respx.get(f"{API}/queries/{QID}/rows").mock(
        side_effect=[httpx.Response(200, json=_rows_page([1, 2], "2", 9))]
    )
    page = execute.fetch_rows(client, QID, limit=2)
    assert len(page["rows"]) == 2
    assert page["truncated"] is True
    assert route.call_count == 1


@respx.mock
def test_a_ddl_statement_with_no_result_file_is_an_empty_page(client):
    """The server answers 200 with no rows rather than 404 for DDL."""
    respx.get(f"{API}/queries/{QID}/rows").mock(
        return_value=httpx.Response(200, json=_rows_page([], None, 0))
    )
    page = execute.fetch_rows(client, QID)
    assert page["rows"] == []
    assert page["truncated"] is False


# --- Waiting ---------------------------------------------------------------


@respx.mock
def test_wait_polls_until_terminal(client, monkeypatch):
    monkeypatch.setattr(execute.time, "sleep", lambda _s: None)
    respx.get(f"{API}/queries/{QID}").mock(
        side_effect=[
            httpx.Response(200, json=_query("queued")),
            httpx.Response(200, json=_query("running")),
            httpx.Response(200, json=_query("done")),
        ]
    )
    assert execute.wait(client, QID, timeout=60)["status"] == "done"


@respx.mock
def test_ctrl_c_cancels_the_query_server_side(client, monkeypatch):
    """Otherwise the statement keeps burning an agent with nobody watching."""

    def _interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(execute.time, "sleep", _interrupt)
    respx.get(f"{API}/queries/{QID}").mock(return_value=httpx.Response(200, json=_query("running")))
    cancelled = respx.delete(f"{API}/queries/{QID}").mock(return_value=httpx.Response(204))
    with pytest.raises(KeyboardInterrupt):
        execute.wait(client, QID, timeout=60)
    assert cancelled.called


@respx.mock
def test_a_client_timeout_cancels_before_giving_up(client, monkeypatch):
    monkeypatch.setattr(execute.time, "sleep", lambda _s: None)
    respx.get(f"{API}/queries/{QID}").mock(return_value=httpx.Response(200, json=_query("running")))
    cancelled = respx.delete(f"{API}/queries/{QID}").mock(return_value=httpx.Response(204))
    from dh.errors import TimeoutError as DhTimeoutError

    with pytest.raises(DhTimeoutError):
        execute.wait(client, QID, timeout=0)
    assert cancelled.called


@respx.mock
def test_cancel_never_raises(client):
    """Best effort by contract; a failure here would mask the real error."""
    respx.delete(f"{API}/queries/{QID}").mock(return_value=httpx.Response(503, json={}))
    execute.cancel(client, QID)


# --- Terminal status -------------------------------------------------------


def test_a_failed_query_exits_6_not_1():
    """The branch a pipeline needs: bad SQL, not a broken CLI."""
    with pytest.raises(Exception) as exc:
        execute.raise_for_status(_query("failed", error="Binder Error: no such column"))
    assert exc.value.exit_code is ExitCode.QUERY_FAILED
    assert "Binder Error" in exc.value.message


def test_a_cancelled_query_is_a_conflict_not_a_failure():
    with pytest.raises(ConflictError):
        execute.raise_for_status(_query("cancelled"))


# --- Agent resolution ------------------------------------------------------


@respx.mock
def test_an_agent_uuid_is_used_without_a_lookup(client):
    assert execute.resolve_agent(client, AGENT) == AGENT
    assert not respx.calls


@respx.mock
def test_an_agent_name_is_resolved_to_its_id(client):
    """Something the CLI can do and a DB-API driver should not."""
    respx.get(f"{API}/agents").mock(
        return_value=httpx.Response(200, json=[{"id": AGENT, "name": "warm"}])
    )
    assert execute.resolve_agent(client, "warm") == AGENT


@respx.mock
def test_an_unknown_agent_name_lists_the_candidates(client):
    respx.get(f"{API}/agents").mock(
        return_value=httpx.Response(200, json=[{"id": AGENT, "name": "warm"}])
    )
    with pytest.raises(ConflictError) as exc:
        execute.resolve_agent(client, "cold")
    assert "warm" in exc.value.message


@respx.mock
def test_a_lone_agent_is_chosen_without_being_named(client):
    respx.get(f"{API}/agents").mock(
        return_value=httpx.Response(200, json=[{"id": AGENT, "name": "only"}])
    )
    assert execute.resolve_agent(client, None) == AGENT


@respx.mock
def test_no_agent_is_chosen_when_several_could_serve(client):
    """Leave it to the server's elastic pool rather than picking arbitrarily."""
    respx.get(f"{API}/agents").mock(
        return_value=httpx.Response(
            200, json=[{"id": AGENT, "name": "a"}, {"id": "x", "name": "b"}]
        )
    )
    assert execute.resolve_agent(client, None) is None


# --- dh sql end to end -----------------------------------------------------


@respx.mock
def test_dh_sql_runs_and_prints_rows(logged_in, monkeypatch):
    monkeypatch.setattr(execute.time, "sleep", lambda _s: None)
    _mock_run(statuses=("done",), rows=[1])
    result = runner.invoke(app, ["--format", "json", "sql", "-q", "select 1"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["columns"] == ["n"]
    assert data["rows"] == [{"n": 1}]


@respx.mock
def test_dh_sql_sends_the_timeout_as_the_server_budget_too(logged_in, monkeypatch):
    """One flag, both meanings; two timeouts would drift."""
    monkeypatch.setattr(execute.time, "sleep", lambda _s: None)
    submitted = respx.post(f"{API}/workspaces/analytics/queries").mock(
        return_value=httpx.Response(202, json=_query("queued"))
    )
    respx.get(f"{API}/queries/{QID}").mock(return_value=httpx.Response(200, json=_query("done")))
    respx.get(f"{API}/queries/{QID}/rows").mock(
        return_value=httpx.Response(200, json=_rows_page([1], None, 1))
    )
    runner.invoke(app, ["sql", "-q", "select 1", "--timeout", "5m"])
    assert json.loads(submitted.calls[0].request.content)["timeout_s"] == 300


@respx.mock
def test_dh_sql_no_wait_returns_the_id_without_polling(logged_in):
    submitted = respx.post(f"{API}/workspaces/analytics/queries").mock(
        return_value=httpx.Response(202, json=_query("queued"))
    )
    polled = respx.get(f"{API}/queries/{QID}").mock(
        return_value=httpx.Response(200, json=_query("done"))
    )
    result = runner.invoke(app, ["--format", "json", "sql", "-q", "select 1", "--no-wait"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["id"] == QID
    assert submitted.called
    assert not polled.called


@respx.mock
def test_dh_sql_exits_6_when_the_query_fails(logged_in, monkeypatch):
    monkeypatch.setattr(execute.time, "sleep", lambda _s: None)
    respx.post(f"{API}/workspaces/analytics/queries").mock(
        return_value=httpx.Response(202, json=_query("queued"))
    )
    respx.get(f"{API}/queries/{QID}").mock(
        return_value=httpx.Response(200, json=_query("failed", error="Binder Error"))
    )
    result = runner.invoke(app, ["sql", "-q", "select nope"])
    assert result.exit_code == ExitCode.QUERY_FAILED


@respx.mock
def test_dh_sql_reads_from_a_file(logged_in, tmp_path, monkeypatch):
    monkeypatch.setattr(execute.time, "sleep", lambda _s: None)
    script = tmp_path / "q.sql"
    script.write_text("select 1\n", encoding="utf-8")
    submitted = respx.post(f"{API}/workspaces/analytics/queries").mock(
        return_value=httpx.Response(202, json=_query("queued"))
    )
    respx.get(f"{API}/queries/{QID}").mock(return_value=httpx.Response(200, json=_query("done")))
    respx.get(f"{API}/queries/{QID}/rows").mock(
        return_value=httpx.Response(200, json=_rows_page([1], None, 1))
    )
    runner.invoke(app, ["sql", "-f", str(script)])
    assert json.loads(submitted.calls[0].request.content)["sql"] == "select 1\n"


@respx.mock
def test_dh_sql_reads_from_stdin(logged_in, monkeypatch):
    monkeypatch.setattr(execute.time, "sleep", lambda _s: None)
    submitted = respx.post(f"{API}/workspaces/analytics/queries").mock(
        return_value=httpx.Response(202, json=_query("queued"))
    )
    respx.get(f"{API}/queries/{QID}").mock(return_value=httpx.Response(200, json=_query("done")))
    respx.get(f"{API}/queries/{QID}/rows").mock(
        return_value=httpx.Response(200, json=_rows_page([1], None, 1))
    )
    runner.invoke(app, ["sql", "-i"], input="select 42")
    assert json.loads(submitted.calls[0].request.content)["sql"] == "select 42"


def test_dh_sql_without_any_sql_says_how_to_supply_it(logged_in):
    result = runner.invoke(app, ["sql"])
    assert result.exit_code == ExitCode.CONFLICT
    assert "--query" in result.output


def test_dh_sql_with_two_sources_is_refused(logged_in, tmp_path):
    script = tmp_path / "q.sql"
    script.write_text("select 1", encoding="utf-8")
    result = runner.invoke(app, ["sql", "-q", "select 1", "-f", str(script)])
    assert result.exit_code == ExitCode.CONFLICT


@respx.mock
def test_dh_query_run_is_the_same_command(logged_in, monkeypatch):
    monkeypatch.setattr(execute.time, "sleep", lambda _s: None)
    _mock_run(statuses=("done",), rows=[7])
    result = runner.invoke(app, ["--format", "json", "query", "run", "-q", "select 7"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["rows"] == [{"n": 7}]


# --- dh query subcommands --------------------------------------------------


@respx.mock
def test_query_get_reports_status(logged_in):
    respx.get(f"{API}/queries/{QID}").mock(return_value=httpx.Response(200, json=_query("running")))
    data = json.loads(runner.invoke(app, ["--format", "json", "query", "get", QID]).stdout)["data"]
    assert data["status"] == "running"


@respx.mock
def test_query_cancel_calls_delete(logged_in):
    route = respx.delete(f"{API}/queries/{QID}").mock(return_value=httpx.Response(204))
    assert runner.invoke(app, ["query", "cancel", QID]).exit_code == 0
    assert route.called


@respx.mock
def test_query_profile_passes_through_null(logged_in):
    respx.get(f"{API}/queries/{QID}/profile").mock(return_value=httpx.Response(200, json=None))
    result = runner.invoke(app, ["--format", "json", "query", "profile", QID])
    assert json.loads(result.stdout)["data"] is None
