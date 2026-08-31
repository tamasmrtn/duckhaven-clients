"""`dh session` and the REPL.

The disabled-surface case is the one that matters most: sessions are off by
default, the routes answer 404, and rendering that as "not found" would send the
reader looking for a missing session rather than a missing feature.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
import respx
from typer.testing import CliRunner

from dh import repl
from dh.context import CliContext
from dh.errors import DhError, ExitCode
from dh.main import app
from dh.output import Format

runner = CliRunner()

HOST = "https://duckhaven.test"
API = f"{HOST}/api"
WS = f"{API}/workspaces/analytics"
SID = "aaaaaaaa-1111-2222-3333-444444444444"
QID = "bbbbbbbb-1111-2222-3333-444444444444"

DISABLED = {"error": "not_found", "message": "SQL sessions are not enabled"}


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


# --- Lifecycle -------------------------------------------------------------


@respx.mock
def test_session_open_reports_the_id_and_status(logged_in):
    respx.get(f"{API}/agents").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{WS}/sql/sessions").mock(
        return_value=httpx.Response(201, json={"id": SID, "status": "open"})
    )
    result = runner.invoke(app, ["--format", "json", "session", "open"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["id"] == SID


@respx.mock
def test_session_open_no_wait_asks_the_server_to_continue(logged_in):
    respx.get(f"{API}/agents").mock(return_value=httpx.Response(200, json=[]))
    route = respx.post(f"{WS}/sql/sessions").mock(
        return_value=httpx.Response(202, json={"id": SID, "status": "pending"})
    )
    runner.invoke(app, ["session", "open", "--no-wait"])
    assert json.loads(route.calls[0].request.content)["on_wait_timeout"] == "continue"


@respx.mock
def test_session_get_and_close(logged_in):
    respx.get(f"{API}/sql/sessions/{SID}").mock(
        return_value=httpx.Response(200, json={"id": SID, "status": "open"})
    )
    closed = respx.delete(f"{API}/sql/sessions/{SID}").mock(return_value=httpx.Response(204))
    assert runner.invoke(app, ["session", "get", SID]).exit_code == 0
    assert runner.invoke(app, ["session", "close", SID]).exit_code == 0
    assert closed.called


@respx.mock
def test_session_statements_are_listed_in_execution_order(logged_in):
    respx.get(f"{API}/sql/sessions/{SID}/statements").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "1"}, {"id": "2"}], "cursor": None, "has_more": False}
        )
    )
    result = runner.invoke(app, ["--format", "json", "session", "statements", SID])
    assert [row["id"] for row in json.loads(result.stdout)["data"]] == ["1", "2"]


@respx.mock
def test_session_exec_runs_and_returns_rows(logged_in, monkeypatch):
    monkeypatch.setattr("dh.execute.time.sleep", lambda _s: None)
    respx.post(f"{API}/sql/sessions/{SID}/statements").mock(
        return_value=httpx.Response(202, json={"id": QID, "status": "queued"})
    )
    respx.get(f"{API}/queries/{QID}").mock(
        return_value=httpx.Response(200, json={"id": QID, "status": "done", "row_count": 1})
    )
    respx.get(f"{API}/queries/{QID}/rows").mock(
        return_value=httpx.Response(
            200, json={"columns": ["n"], "rows": [[1]], "cursor": None, "total": 1}
        )
    )
    result = runner.invoke(app, ["--format", "json", "session", "exec", SID, "-q", "select 1"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["rows"] == [[1]]


# --- Sessions switched off -------------------------------------------------


@respx.mock
@pytest.mark.parametrize(
    ("argv", "route"),
    [
        (["session", "list"], "get"),
        (["session", "get", SID], "get"),
        (["session", "close", SID], "delete"),
    ],
)
def test_a_disabled_surface_says_so_rather_than_not_found(logged_in, argv, route):
    """404 rendered as "not found" sends the reader hunting a missing session."""
    respx.get(f"{WS}/sql/sessions").mock(return_value=httpx.Response(404, json=DISABLED))
    respx.get(f"{API}/sql/sessions/{SID}").mock(return_value=httpx.Response(404, json=DISABLED))
    respx.delete(f"{API}/sql/sessions/{SID}").mock(return_value=httpx.Response(404, json=DISABLED))
    result = runner.invoke(app, argv)
    assert result.exit_code != 0
    assert "not enabled" in result.output
    assert "dh sql" in result.output


@respx.mock
def test_a_genuine_missing_session_still_reads_as_not_found(logged_in):
    respx.get(f"{API}/sql/sessions/{SID}").mock(
        return_value=httpx.Response(
            404, json={"error": "not_found", "message": "Session not found"}
        )
    )
    result = runner.invoke(app, ["session", "get", SID])
    assert result.exit_code == ExitCode.NOT_FOUND
    assert "Session not found" in result.output


# --- REPL statement buffering ----------------------------------------------


def _lines(*lines):
    """A read_line callable that replays lines then raises EOFError."""
    queue = list(lines)

    def read(_prompt):
        if not queue:
            raise EOFError
        return queue.pop(0)

    return read


def test_statements_are_buffered_until_a_semicolon():
    """A REPL that submitted every line could not take pasted multi-line SQL."""
    got = list(repl.read_statements(_lines("select 1,", "       2;")))
    assert got == ["select 1,\n       2"]


def test_several_statements_come_back_separately():
    assert list(repl.read_statements(_lines("select 1;", "select 2;"))) == ["select 1", "select 2"]


def test_a_trailing_unterminated_statement_is_still_yielded():
    """Ending input mid-statement should run what was typed, not discard it."""
    assert list(repl.read_statements(_lines("select 1"))) == ["select 1"]


@pytest.mark.parametrize("word", ["\\q", "quit", "exit", "EXIT"])
def test_the_quit_words_end_the_shell(word):
    assert list(repl.read_statements(_lines(word, "select 1;"))) == []


def test_blank_lines_between_statements_are_ignored():
    assert list(repl.read_statements(_lines("", "  ", "select 1;"))) == ["select 1"]


# --- REPL fallback ---------------------------------------------------------


class _FailingConnect:
    def __call__(self, **_kwargs):
        raise RuntimeError("sessions are not enabled")


def test_the_repl_falls_back_when_a_session_cannot_be_opened(monkeypatch, capsys):
    """A stateless shell beats a shell that refuses to start."""
    import duckhaven_sql_connector

    monkeypatch.setattr(duckhaven_sql_connector, "connect", _FailingConnect())
    cli = CliContext(fmt=Format.JSON)
    settings = _settings()
    monkeypatch.setattr(repl, "_loop", lambda *a, **k: 0)
    assert repl.run(cli, settings) == 0
    assert "not enabled" in capsys.readouterr().err


def test_the_session_is_closed_even_when_the_loop_raises(monkeypatch):
    """An unclosed session holds an agent slot until the reaper notices."""
    closed = []

    class _Conn:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(repl, "_open_session", lambda *a, **k: _Conn())
    monkeypatch.setattr(repl, "_loop", lambda *a, **k: (_ for _ in ()).throw(DhError("x", "y")))
    with pytest.raises(DhError):
        repl.run(CliContext(), _settings())
    assert closed == [True]


def _settings():
    from dh.config import Config, Profile
    from dh.resolve import resolve

    return resolve(
        Config(
            path=__import__("pathlib").Path("/nonexistent"),
            profiles={
                "default": Profile(
                    name="default", host=HOST, token="dh_pat_x", workspace="analytics"
                )
            },
        ),
        {},
    )
