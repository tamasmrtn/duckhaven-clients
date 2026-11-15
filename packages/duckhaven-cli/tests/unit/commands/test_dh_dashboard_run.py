"""`dh dashboard test`, `preview` and `impact` — running files without saving them.

What carries the weight: each file is held as a preview and run as the person
running dh, every widget is reported and a failed or unreadable one fails the
run, and `preview` keeps the preview following the file, starting a new one
when the last has ended.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest
import respx
from typer.testing import CliRunner

from dh.commands import dashboard
from dh.errors import ExitCode
from dh.main import app

runner = CliRunner()

HOST = "https://duckhaven.test"
WS = f"{HOST}/api/workspaces/analytics"
PREVIEWS = f"{WS}/dashboard-previews"

SALES = b"version: 1\nname: sales\ntitle: Sales\nrows: []\n"
NOTES = b"version: 1\nname: notes\ntitle: Notes\nrows: []\n"


def _held(preview_id: str = "p1", warnings: list | None = None) -> httpx.Response:
    """A preview as `POST` or `PUT .../dashboard-previews` answers it."""
    body = {
        "id": preview_id,
        "spec": {},
        "source_text": "",
        "spec_hash": "h",
        "created_at": "2026-11-15T10:00:00Z",
        "updated_at": "2026-11-15T10:00:00Z",
        "expires_at": "2026-11-15T12:00:00Z",
        "warnings": warnings or [],
    }
    return httpx.Response(201, json=body)


def _widget(widget_id: str, state: str = "ok", *, elapsed: int = 120, **extra) -> dict:
    event = {
        "type": "widget",
        "id": widget_id,
        "state": state,
        "scope": {"window": None, "compare_window": None, "applied": [], "skipped": []},
        "timing": {"elapsed_ms": elapsed, "query_ms": 80},
        **extra,
    }
    if state in ("ok", "empty"):
        event.setdefault("cache", "miss")
        event.setdefault("rows", {"columns": [{"name": "revenue"}], "data": [[12]]})
    return event


def _done(**states: int) -> dict:
    return {"type": "done", "elapsed_ms": 300, "widgets": states, "statements": 1}


def _stream(*events: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    return httpx.Response(
        200, content=b": ping\n\n" + body.encode(), headers={"content-type": "text/event-stream"}
    )


def _invalid(message: str, line: int, col: int) -> httpx.Response:
    diagnostic = {"path": "rows[0]", "line": line, "col": col, "message": message, "hint": None}
    return httpx.Response(
        422,
        json={
            "error": "invalid_dashboard",
            "message": message,
            "details": {"diagnostics": [diagnostic]},
        },
    )


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "dashboards"
    (root / "team").mkdir(parents=True)
    (root / "sales.dashboard.yml").write_bytes(SALES)
    (root / "team" / "notes.dashboard.yml").write_bytes(NOTES)
    return root


def _rows(result) -> list[dict]:
    return json.loads(result.stdout)["data"]


# --- test ------------------------------------------------------------------


@respx.mock
def test_test_runs_each_file_as_a_preview_and_reports_every_widget(logged_in, repo):
    create = respx.post(PREVIEWS).mock(side_effect=[_held("p1"), _held("p2")])
    sales = respx.post(f"{PREVIEWS}/p1/run").mock(
        return_value=_stream(
            {"type": "start"}, _widget("revenue"), _widget("orders", "empty"), _done(ok=1, empty=1)
        )
    )
    respx.post(f"{PREVIEWS}/p2/run").mock(return_value=_stream(_widget("memo"), _done(ok=1)))

    result = runner.invoke(app, ["dashboard", "test", str(repo)])

    assert result.exit_code == 0, result.output
    sent = [json.loads(call.request.content) for call in create.calls]
    assert sent == [{"source_text": SALES.decode()}, {"source_text": NOTES.decode()}]
    assert json.loads(sales.calls[0].request.content) == {"use_cache": True}
    rows = _rows(result)
    assert [(r["name"], r["widget"], r["state"]) for r in rows] == [
        ("sales", "revenue", "ok"),
        ("sales", "orders", "empty"),
        ("notes", "memo", "ok"),
    ]
    assert rows[0] | {"file": None} == {
        "file": None,
        "name": "sales",
        "widget": "revenue",
        "state": "ok",
        "slow": False,
        "elapsed_ms": 120,
        "query_ms": 80,
        "cache": "miss",
        "error": None,
    }
    assert "sales.dashboard.yml: revenue: ok in 0.1 s, cache miss" in result.stderr
    assert "sales.dashboard.yml: 2 widgets, 1 empty, 1 ok, in 0.3 s." in result.stderr


@respx.mock
def test_test_fails_with_6_when_a_widget_fails_or_cannot_be_read(logged_in, tmp_path):
    file = tmp_path / "sales.dashboard.yml"
    file.write_bytes(SALES)
    respx.post(PREVIEWS).mock(return_value=_held())
    respx.post(f"{PREVIEWS}/p1/run").mock(
        return_value=_stream(
            _widget("revenue"),
            _widget("orders", "error", error="Binder Error: no column amount"),
            _widget("refunds", "no_access", error="You may not read what this widget reads."),
            _done(ok=1, error=1, no_access=1),
        )
    )

    result = runner.invoke(app, ["dashboard", "test", str(file)])

    assert result.exit_code == ExitCode.QUERY_FAILED
    assert "orders: error: Binder Error: no column amount" in result.stderr
    assert "refunds: no_access: You may not read" in result.stderr
    assert "2 of 3 widgets in" in result.stderr
    assert "failed: orders, refunds" in result.stderr
    assert [r["error"] for r in _rows(result)][1] == "Binder Error: no column amount"


@respx.mock
def test_test_runs_every_file_and_exits_with_the_first_failure(logged_in, repo):
    respx.post(PREVIEWS).mock(side_effect=[_invalid("No metric 'revnue'.", 4, 9), _held("p2")])
    respx.post(f"{PREVIEWS}/p2/run").mock(
        return_value=_stream(_widget("memo", "error", error="boom"), _done(error=1))
    )

    result = runner.invoke(app, ["dashboard", "test", str(repo)])

    assert result.exit_code == ExitCode.CONFLICT
    assert "sales.dashboard.yml:4:9: error: No metric 'revnue'. (rows[0])" in result.stderr
    assert "memo: error: boom" in result.stderr
    assert "2 of 2 dashboard files failed" in result.stderr
    assert _rows(result)[0]["error"]["error"] == "invalid_dashboard"


@respx.mock
def test_test_one_invalid_file_fails_with_the_servers_error_once(logged_in, tmp_path):
    file = tmp_path / "sales.dashboard.yml"
    file.write_bytes(SALES)
    respx.post(PREVIEWS).mock(
        return_value=httpx.Response(
            403, json={"error": "http_403", "message": "Writer only.", "details": None}
        )
    )

    result = runner.invoke(app, ["dashboard", "test", str(file)])

    assert result.exit_code == ExitCode.AUTH
    assert result.stderr.count("Writer only.") == 1


@respx.mock
def test_test_no_cache_and_slow(logged_in, tmp_path):
    file = tmp_path / "sales.dashboard.yml"
    file.write_bytes(SALES)
    respx.post(PREVIEWS).mock(return_value=_held())
    run = respx.post(f"{PREVIEWS}/p1/run").mock(
        return_value=_stream(
            {"type": "compute", "state": "starting", "widgets": ["revenue"]},
            _widget("revenue", elapsed=6200),
            _widget("orders", elapsed=200),
            _done(ok=2),
        )
    )

    result = runner.invoke(app, ["dashboard", "test", str(file), "--no-cache", "--slow", "1"])

    assert result.exit_code == 0, result.output
    assert json.loads(run.calls[0].request.content) == {"use_cache": False}
    assert [r["slow"] for r in _rows(result)] == [True, False]
    assert "revenue: ok, slow in 6.2 s" in result.stderr
    assert "Starting compute for revenue." in result.stderr


@respx.mock
@pytest.mark.parametrize(
    ("events", "status", "message"),
    [
        (
            [{"type": "error", "message": "The dashboard could not be run."}],
            ExitCode.FAILURE,
            "could not be run",
        ),
        ([_widget("revenue")], ExitCode.UNAVAILABLE, "ended before the run finished"),
    ],
    ids=["error-event", "cut-short"],
)
def test_test_a_run_that_cannot_finish(logged_in, tmp_path, events, status, message):
    file = tmp_path / "sales.dashboard.yml"
    file.write_bytes(SALES)
    respx.post(PREVIEWS).mock(return_value=_held())
    respx.post(f"{PREVIEWS}/p1/run").mock(return_value=_stream(*events))

    result = runner.invoke(app, ["dashboard", "test", str(file)])

    assert result.exit_code == status
    assert message in result.output


def test_test_refuses_a_file_that_is_not_utf8(logged_in, tmp_path):
    file = tmp_path / "sales.dashboard.yml"
    file.write_bytes(b"title: \xff\n")

    result = runner.invoke(app, ["dashboard", "test", str(file)])

    assert result.exit_code == ExitCode.CONFLICT
    assert "is not UTF-8" in result.output


# --- preview --once ----------------------------------------------------------


@respx.mock
def test_preview_once_prints_each_widget_and_the_page(logged_in, tmp_path, monkeypatch):
    file = tmp_path / "sales.dashboard.yml"
    file.write_bytes(SALES)
    opened: list[str] = []
    monkeypatch.setattr(dashboard.webbrowser, "open", opened.append)
    warning = {"path": "rows[0]", "line": 3, "col": 1, "message": "Many bars.", "hint": None}
    respx.post(PREVIEWS).mock(return_value=_held(warnings=[warning]))
    respx.post(f"{PREVIEWS}/p1/run").mock(
        return_value=_stream(_widget("revenue", sql="SELECT 1"), _done(ok=1))
    )

    result = runner.invoke(app, ["dashboard", "preview", str(file), "--once", "--open"])

    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout)["data"]
    page = f"{HOST}/analytics/dashboard-previews/p1"
    assert shown["url"] == page
    assert opened == [page]
    assert shown["id"] == "p1"
    assert shown["warnings"] == [warning]
    [widget] = shown["widgets"]
    assert widget["rows"]["data"] == [[12]]
    assert widget["sql"] == "SELECT 1"
    assert "type" not in widget
    assert shown["done"]["widgets"] == {"ok": 1}
    assert "sales.dashboard.yml:3:1: warning: Many bars." in result.stderr


@respx.mock
def test_preview_once_exits_6_when_a_widget_fails(logged_in, tmp_path):
    file = tmp_path / "sales.dashboard.yml"
    file.write_bytes(SALES)
    respx.post(PREVIEWS).mock(return_value=_held())
    respx.post(f"{PREVIEWS}/p1/run").mock(
        return_value=_stream(_widget("revenue", "error", error="boom"), _done(error=1))
    )

    result = runner.invoke(app, ["dashboard", "preview", str(file), "--once"])

    assert result.exit_code == ExitCode.QUERY_FAILED
    assert json.loads(result.stdout)["data"]["widgets"][0]["error"] == "boom"


@respx.mock
def test_preview_once_reports_an_invalid_file_at_its_line(logged_in, tmp_path):
    file = tmp_path / "sales.dashboard.yml"
    file.write_bytes(SALES)
    respx.post(PREVIEWS).mock(return_value=_invalid("No metric 'revnue'.", 4, 9))

    result = runner.invoke(app, ["dashboard", "preview", str(file), "--once"])

    assert result.exit_code == ExitCode.CONFLICT
    assert "sales.dashboard.yml:4:9: error: No metric 'revnue'." in result.stderr


def test_preview_refuses_a_file_not_named_as_a_dashboard(logged_in, tmp_path):
    file = tmp_path / "sales.yml"
    file.write_bytes(SALES)

    result = runner.invoke(app, ["dashboard", "preview", str(file), "--once"])

    assert result.exit_code == ExitCode.CONFLICT
    assert "is not named <name>.dashboard.yml" in result.output


# --- preview, following the file ---------------------------------------------


@respx.mock
def test_preview_follows_each_save_and_starts_again_once_ended(logged_in, tmp_path, monkeypatch):
    file = tmp_path / "sales.dashboard.yml"
    file.write_bytes(SALES)
    saves = [SALES, b"title: [\n", b"\xff", SALES + b"# two\n", SALES + b"# three\n"]
    monkeypatch.setattr(dashboard, "_saves", lambda _file: iter(saves))
    opened: list[str] = []
    monkeypatch.setattr(dashboard.webbrowser, "open", opened.append)
    create = respx.post(PREVIEWS).mock(side_effect=[_held("p1"), _held("p2")])
    replace = respx.put(f"{PREVIEWS}/p1").mock(
        side_effect=[
            _invalid("Expected a value.", 1, 9),
            _held("p1"),
            httpx.Response(404, json={"error": "not_found", "message": "No preview p1."}),
        ]
    )

    result = runner.invoke(app, ["dashboard", "preview", str(file), "--open"])

    assert result.exit_code == 0, result.output
    assert [json.loads(c.request.content)["source_text"] for c in create.calls] == [
        SALES.decode(),
        (SALES + b"# three\n").decode(),
    ]
    # A file that is not text is reported, and never sent.
    assert [json.loads(c.request.content)["source_text"] for c in replace.calls] == [
        s.decode() for s in (saves[1], saves[3], saves[4])
    ]
    assert f"\n{file} is not UTF-8.\n" in result.stderr
    pages = [f"{HOST}/analytics/dashboard-previews/{p}" for p in ("p1", "p2")]
    assert opened == pages
    assert all(page in result.stdout for page in pages)
    assert "sales.dashboard.yml:1:9: error: Expected a value." in result.stderr
    assert "keeps showing the last file that loaded" in result.stderr
    assert "sales.dashboard.yml: sent" in result.stderr
    assert "The preview ended, so this save starts a new one." in result.stderr


@respx.mock
def test_preview_waits_for_a_file_that_loads(logged_in, tmp_path, monkeypatch):
    file = tmp_path / "sales.dashboard.yml"
    file.write_bytes(SALES)
    monkeypatch.setattr(dashboard, "_saves", lambda _file: iter([b"title: [\n", SALES]))
    create = respx.post(PREVIEWS).mock(
        side_effect=[_invalid("Expected a value.", 1, 9), _held("p1")]
    )

    result = runner.invoke(app, ["dashboard", "preview", str(file)])

    assert result.exit_code == 0, result.output
    assert create.call_count == 2
    assert "Nothing to show until the file loads" in result.stderr
    assert f"{HOST}/analytics/dashboard-previews/p1" in result.stdout


def test_saves_yields_the_file_then_each_new_save(tmp_path, monkeypatch):
    """A save with nothing new is skipped, and a file gone for a moment is waited out."""
    file = tmp_path / "sales.dashboard.yml"
    file.write_bytes(b"one")

    def write(body: bytes, stamp: int) -> None:
        file.write_bytes(body)
        os.utime(file, ns=(stamp, stamp))

    class Stop(Exception):
        pass

    steps = iter(
        [
            lambda: None,  # not saved
            lambda: write(b"one", 2_000_000_000),  # saved again, unchanged
            lambda: write(b"two", 3_000_000_000),
            file.unlink,  # an editor replacing the file
            lambda: write(b"three", 4_000_000_000),
        ]
    )

    def sleep(_seconds: float) -> None:
        step = next(steps, None)
        if step is None:
            raise Stop
        step()

    monkeypatch.setattr(dashboard.time, "sleep", sleep)
    seen: list[bytes] = []
    with pytest.raises(Stop):
        for body in dashboard._saves(file):
            seen.append(body)
    assert seen == [b"one", b"two", b"three"]


# --- impact ----------------------------------------------------------------


@respx.mock
def test_impact_lists_every_dashboard_that_reads_a_ref(logged_in):
    summary = {
        "name": "sales",
        "title": "Sales",
        "status": "published",
        "has_draft": True,
        "source": "git",
        "source_id": "acme/bi",
        "managed": True,
        "version": 4,
    }
    route = respx.get(f"{WS}/dashboards").mock(
        side_effect=[
            httpx.Response(200, json={"items": [summary], "cursor": "c2", "has_more": True}),
            httpx.Response(
                200,
                json={"items": [{**summary, "name": "ops"}], "cursor": None, "has_more": False},
            ),
        ]
    )

    result = runner.invoke(app, ["dashboard", "impact", "orders.revenue"])

    assert result.exit_code == 0, result.output
    assert route.calls[0].request.url.params["uses"] == "orders.revenue"
    assert route.calls[1].request.url.params["cursor"] == "c2"
    rows = _rows(result)
    assert [r["name"] for r in rows] == ["sales", "ops"]
    assert set(rows[0]) == {"name", "title", "status", "has_draft", "source", "source_id"}
    assert "2 dashboards read orders.revenue." in result.stderr


@respx.mock
def test_impact_says_when_nothing_reads_it(logged_in):
    respx.get(f"{WS}/dashboards").mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None, "has_more": False})
    )

    result = runner.invoke(app, ["dashboard", "impact", "orders"])

    assert result.exit_code == 0
    assert _rows(result) == []
    assert "No dashboard reads orders." in result.stderr
