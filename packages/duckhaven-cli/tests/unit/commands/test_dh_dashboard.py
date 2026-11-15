"""`dh dashboard` — dashboard files applied from git.

What carries the weight: each file reaches the dashboard it is named after,
byte for byte as YAML; a run applies every file and reports each failure at
its line; and `--prune` deletes nothing it was not told to, and nothing at all
when a file failed or nobody agreed.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from typer.testing import CliRunner

from dh import context
from dh.errors import ExitCode
from dh.main import app

runner = CliRunner()

WS = "https://duckhaven.test/api/workspaces/analytics"
BOARDS = f"{WS}/dashboards"

SALES = b"# Revenue by week.\nversion: 1\nname: sales\ntitle: Sales\nrows: []\n"
NOTES = b"version: 1\nname: notes\ntitle: Notes\nrows: []\n"


def _answer(name: str, **extra) -> dict:
    """A server's apply answer, as `PUT .../source` returns it."""
    return {
        "dry_run": False,
        "action": "update",
        "name": name,
        "renamed_from": None,
        "adopted": False,
        "published": False,
        "current_hash": "old",
        "spec_hash": "new",
        "changes": [{"path": "title", "change": "changed"}],
        "warnings": [],
        "dashboard": {"name": name, "spec": {}},
        **extra,
    }


def _invalid(message: str, line: int, col: int, hint: str | None = None) -> httpx.Response:
    diagnostic = {"path": "rows[0]", "line": line, "col": col, "message": message, "hint": hint}
    details = {"diagnostics": [diagnostic]}
    return httpx.Response(
        422, json={"error": "invalid_dashboard", "message": message, "details": details}
    )


def _refused(status: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": code, "message": message, "details": None})


@pytest.fixture
def repo(tmp_path):
    """A repository's dashboards: two files, one a level down, beside a README."""
    root = tmp_path / "dashboards"
    (root / "team").mkdir(parents=True)
    (root / "sales.dashboard.yml").write_bytes(SALES)
    (root / "team" / "notes.dashboard.yml").write_bytes(NOTES)
    (root / "README.md").write_text("Not a dashboard.\n", encoding="utf-8")
    return root


def _rows(result) -> list[dict]:
    return json.loads(result.stdout)["data"]


# --- Which files, and under which name -------------------------------------


@respx.mock
def test_apply_sends_each_file_byte_for_byte_to_the_dashboard_it_is_named_after(logged_in, repo):
    """The server keeps the bytes, so a comment survives and an error has a line."""
    sales = respx.put(f"{BOARDS}/sales/source").mock(
        return_value=httpx.Response(200, json=_answer("sales"))
    )
    notes = respx.put(f"{BOARDS}/notes/source").mock(
        return_value=httpx.Response(201, json=_answer("notes", action="create"))
    )

    result = runner.invoke(
        app,
        ["dashboard", "apply", str(repo), "--publish", "--source-id", "acme/bi"]
        + ["--source-ref", "3f2a1c9"],
    )

    assert result.exit_code == 0, result.output
    request = sales.calls[0].request
    assert request.content == SALES
    assert request.headers["Content-Type"] == "application/yaml"
    assert dict(request.url.params) == {
        "publish": "true",
        "source_id": "acme/bi",
        "source_ref": "3f2a1c9",
    }
    assert notes.calls[0].request.content == NOTES
    rows = _rows(result)
    assert [(r["file"], r["action"]) for r in rows] == [
        (str(repo / "sales.dashboard.yml"), "update"),
        (str(repo / "team" / "notes.dashboard.yml"), "create"),
    ]
    assert "dashboard" not in rows[0]
    assert "sales: update: title changed" in result.stderr


@respx.mock
def test_flags_left_out_are_not_sent(logged_in, repo):
    """The server's defaults apply: a draft, a real apply, the source kept."""
    route = respx.put(f"{BOARDS}/sales/source").mock(
        return_value=httpx.Response(200, json=_answer("sales"))
    )

    runner.invoke(app, ["dashboard", "apply", str(repo / "sales.dashboard.yml")])

    assert dict(route.calls[0].request.url.params) == {}


@respx.mock
def test_dry_run_force_and_expected_hash_are_sent(logged_in, repo):
    route = respx.put(f"{BOARDS}/sales/source").mock(
        return_value=httpx.Response(200, json=_answer("sales", dry_run=True))
    )
    argv = ["dashboard", "apply", str(repo / "sales.dashboard.yml"), "--dry-run", "--force"]

    result = runner.invoke(app, [*argv, "--expected-hash", "abc"])

    assert result.exit_code == 0, result.output
    params = route.calls[0].request.url.params
    assert (params["dry_run"], params["force"], params["expected_hash"]) == ("true", "true", "abc")
    assert "dry run" in result.stderr


@respx.mock
def test_a_file_not_named_after_a_dashboard_is_refused_before_anything_is_sent(logged_in, repo):
    result = runner.invoke(app, ["dashboard", "apply", str(repo / "README.md")])

    assert result.exit_code == ExitCode.CONFLICT
    assert "<name>.dashboard.yml" in result.output
    assert respx.calls.call_count == 0


@respx.mock
def test_two_files_naming_one_dashboard_are_refused(logged_in, repo):
    """Names match ignoring case, so these would apply over each other."""
    (repo / "team" / "Sales.dashboard.yml").write_bytes(SALES)

    result = runner.invoke(app, ["dashboard", "apply", str(repo)])

    assert result.exit_code == ExitCode.CONFLICT
    assert "name the same dashboard" in result.output
    assert respx.calls.call_count == 0


@respx.mock
def test_a_file_given_twice_is_applied_once(logged_in, repo):
    route = respx.put(f"{BOARDS}/sales/source").mock(
        return_value=httpx.Response(200, json=_answer("sales"))
    )
    file = str(repo / "sales.dashboard.yml")

    assert runner.invoke(app, ["dashboard", "apply", file, file]).exit_code == 0
    assert route.call_count == 1


@respx.mock
def test_a_directory_with_no_dashboard_files_is_refused(logged_in, tmp_path):
    result = runner.invoke(app, ["dashboard", "apply", str(tmp_path)])

    assert result.exit_code == ExitCode.CONFLICT
    assert "No *.dashboard.yml files" in result.output


def test_expected_hash_checks_one_file_only(logged_in, repo):
    result = runner.invoke(app, ["dashboard", "apply", str(repo), "--expected-hash", "abc"])

    assert result.exit_code == ExitCode.CONFLICT
    assert "one file" in result.output


# --- Failures --------------------------------------------------------------


@respx.mock
def test_every_file_is_applied_and_each_problem_reported_at_its_line(logged_in, repo):
    """The first failure does not hide the rest, and `file:line:col` points at it."""
    respx.put(f"{BOARDS}/sales/source").mock(
        return_value=_invalid("'shop' has no metric called 'revnue'.", 12, 16, "Did you mean?")
    )
    notes = respx.put(f"{BOARDS}/notes/source").mock(
        return_value=httpx.Response(200, json=_answer("notes"))
    )

    result = runner.invoke(app, ["dashboard", "apply", str(repo)])

    assert result.exit_code == ExitCode.CONFLICT
    assert notes.called
    file = repo / "sales.dashboard.yml"
    assert f"{file}:12:16: error: 'shop' has no metric called 'revnue'. (rows[0])" in result.stderr
    assert "    Did you mean?" in result.stderr
    assert f"1 of 2 dashboard files did not apply: {file}" in result.stderr
    failed, applied = _rows(result)
    assert failed["error"]["error"] == "invalid_dashboard"
    assert (applied["action"], applied["error"]) == ("update", None)


@respx.mock
@pytest.mark.parametrize(
    ("status", "code", "exit_code"),
    [
        (409, "source_changed", ExitCode.CONFLICT),
        (409, "managed_dashboard", ExitCode.CONFLICT),
        (403, "http_403", ExitCode.AUTH),
    ],
)
def test_one_file_fails_with_the_servers_own_error(logged_in, repo, status, code, exit_code):
    respx.put(f"{BOARDS}/sales/source").mock(
        return_value=_refused(status, code, "'sales' is applied from acme/bi.")
    )

    result = runner.invoke(
        app, ["--format", "table", "dashboard", "apply", str(repo / "sales.dashboard.yml")]
    )

    assert result.exit_code == exit_code
    # Once, as the error: alone, a note saying the same would only repeat it.
    assert result.stderr.count("'sales' is applied from acme/bi.") == 1


@respx.mock
def test_several_failures_exit_with_the_firsts_status_and_name_each_file(logged_in, repo):
    respx.put(f"{BOARDS}/sales/source").mock(return_value=_refused(403, "http_403", "Owner only."))
    respx.put(f"{BOARDS}/notes/source").mock(return_value=_invalid("Bad.", 3, 1))

    result = runner.invoke(app, ["dashboard", "apply", str(repo)])

    assert result.exit_code == ExitCode.AUTH
    assert f"{repo / 'sales.dashboard.yml'}: Owner only." in result.stderr
    error = json.loads(result.stderr[result.stderr.index("{") :])
    assert error["error"] == "http_403"
    assert [f["error"] for f in error["details"]["files"]] == ["http_403", "invalid_dashboard"]


# --- Pruning ---------------------------------------------------------------


def _listed(*names: str):
    items = [{"name": name, "source": "git", "source_id": "acme/bi"} for name in names]
    return respx.get(BOARDS).mock(
        return_value=httpx.Response(200, json={"items": items, "cursor": None, "has_more": False})
    )


def _applies():
    for name in ("sales", "notes"):
        respx.put(f"{BOARDS}/{name}/source").mock(
            return_value=httpx.Response(200, json=_answer(name))
        )


@respx.mock
def test_prune_detaches_and_deletes_the_dashboards_whose_files_are_gone(logged_in, repo):
    listed = _listed("sales", "Notes", "refunds")
    _applies()
    detach = respx.delete(f"{BOARDS}/refunds/source").mock(return_value=httpx.Response(204))
    delete = respx.delete(f"{BOARDS}/refunds").mock(return_value=httpx.Response(204))
    argv = ["dashboard", "apply", str(repo), "--prune", "--source-id", "acme/bi", "--yes"]

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert dict(listed.calls[0].request.url.params) == {"source": "git", "source_id": "acme/bi"}
    assert detach.called and delete.called
    assert respx.calls[-1].request.url == delete.calls[0].request.url
    assert _rows(result)[-1] == {
        "file": None,
        "name": "refunds",
        "action": "delete",
        "dry_run": False,
        "error": None,
    }


def test_prune_needs_a_source_id(logged_in, repo):
    result = runner.invoke(app, ["dashboard", "apply", str(repo), "--prune"])

    assert result.exit_code == ExitCode.CONFLICT
    assert "--source-id" in result.output


@respx.mock
def test_an_unattended_prune_without_yes_changes_nothing(logged_in, repo):
    """Refused before the first apply, not halfway through the run."""
    _listed("refunds")
    _applies()

    result = runner.invoke(app, ["dashboard", "apply", str(repo), "--prune", "--source-id", "x"])

    assert result.exit_code == ExitCode.ABORTED
    assert [call.request.method for call in respx.calls] == ["GET"]


@respx.mock
def test_answering_yes_at_the_prompt_prunes(logged_in, repo, monkeypatch):
    monkeypatch.setattr(context, "_interactive", lambda: True)
    _listed("refunds")
    _applies()
    respx.delete(f"{BOARDS}/refunds/source").mock(return_value=httpx.Response(204))
    delete = respx.delete(f"{BOARDS}/refunds").mock(return_value=httpx.Response(204))
    argv = ["dashboard", "apply", str(repo), "--prune", "--source-id", "acme/bi"]

    result = runner.invoke(app, argv, input="y\n")

    assert result.exit_code == 0, result.output
    assert "Detach and delete 1 dashboard(s) applied from acme/bi: refunds?" in result.stderr
    assert delete.called


@respx.mock
def test_a_prune_dry_run_lists_and_deletes_nothing(logged_in, repo):
    _listed("refunds")
    _applies()
    argv = ["dashboard", "apply", str(repo), "--prune", "--source-id", "acme/bi", "--dry-run"]

    result = runner.invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert "DELETE" not in [call.request.method for call in respx.calls]
    assert _rows(result)[-1]["action"] == "delete"
    assert "refunds: deleted, dry run" in result.stderr


@respx.mock
def test_nothing_is_pruned_when_a_file_did_not_apply(logged_in, repo):
    """A broken file may be the one that renames a dashboard; deleting would lose it."""
    _listed("refunds")
    respx.put(f"{BOARDS}/sales/source").mock(return_value=_invalid("Bad.", 1, 1))
    respx.put(f"{BOARDS}/notes/source").mock(
        return_value=httpx.Response(200, json=_answer("notes"))
    )
    argv = ["dashboard", "apply", str(repo), "--prune", "--source-id", "acme/bi", "--yes"]

    result = runner.invoke(app, argv)

    assert result.exit_code == ExitCode.CONFLICT
    assert "DELETE" not in [call.request.method for call in respx.calls]
    assert "Not pruning refunds" in result.stderr


# --- Validating ------------------------------------------------------------


@respx.mock
def test_validate_checks_each_file_and_reports_its_problems_at_their_lines(logged_in, repo):
    found = {
        "valid": False,
        "errors": [{"path": "name", "line": 3, "col": 7, "message": "Bad name.", "hint": None}],
        "warnings": [{"path": "", "line": None, "col": None, "message": "Many.", "hint": None}],
        "refs": [],
    }
    route = respx.post(f"{WS}/dashboard-validations").mock(
        side_effect=[httpx.Response(200, json=found), httpx.Response(200, json={"valid": True})]
    )

    result = runner.invoke(app, ["dashboard", "validate", str(repo)])

    assert result.exit_code == ExitCode.CONFLICT
    assert json.loads(route.calls[0].request.content) == {"source_text": SALES.decode()}
    file = repo / "sales.dashboard.yml"
    assert f"{file}:3:7: error: Bad name. (name)" in result.stderr
    assert f"{file}: warning: Many." in result.stderr
    assert f"{repo / 'team' / 'notes.dashboard.yml'}: valid" in result.stderr
    assert f"1 of 2 dashboard files are not valid: {file}" in result.stderr
    assert [row["valid"] for row in _rows(result)] == [False, True]


@respx.mock
def test_warnings_alone_do_not_fail_validation(logged_in, repo):
    warning = {"path": "rows", "line": 4, "col": 1, "message": "Slow.", "hint": None}
    respx.post(f"{WS}/dashboard-validations").mock(
        return_value=httpx.Response(200, json={"valid": True, "errors": [], "warnings": [warning]})
    )

    result = runner.invoke(app, ["dashboard", "validate", str(repo / "sales.dashboard.yml")])

    assert result.exit_code == 0, result.output
    assert "warning: Slow." in result.stderr


@respx.mock
def test_a_file_that_is_not_utf8_is_reported_and_not_sent(logged_in, repo):
    route = respx.post(f"{WS}/dashboard-validations").mock(
        return_value=httpx.Response(200, json={"valid": True})
    )
    (repo / "sales.dashboard.yml").write_bytes(b"title: caf\xe9\n")

    result = runner.invoke(app, ["dashboard", "validate", str(repo)])

    assert result.exit_code == ExitCode.CONFLICT
    assert "is not UTF-8" in result.stderr
    assert route.call_count == 1


# --- Exporting -------------------------------------------------------------


@respx.mock
def test_export_writes_the_file_as_it_is(logged_in):
    """Not the JSON envelope, and not decoded: this YAML happens to be JSON too."""
    raw = b'{"version": 1, "name": "sales", "title": "Sales", "rows": []}\n'
    route = respx.get(f"{BOARDS}/sales/source").mock(
        return_value=httpx.Response(200, content=raw, headers={"Content-Type": "application/yaml"})
    )

    result = runner.invoke(app, ["dashboard", "export", "sales", "--draft"])

    assert result.exit_code == 0, result.output
    assert result.stdout_bytes == raw
    assert route.calls[0].request.url.params["draft"] == "true"


@respx.mock
def test_export_writes_to_output(logged_in, tmp_path):
    respx.get(f"{BOARDS}/sales/source").mock(return_value=httpx.Response(200, content=SALES))
    target = tmp_path / "sales.dashboard.yml"

    result = runner.invoke(app, ["--output", str(target), "dashboard", "export", "sales"])

    assert result.exit_code == 0, result.output
    assert target.read_bytes() == SALES
    assert result.stdout == ""


@respx.mock
def test_schema_prints_the_schema_itself(logged_in):
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Dashboard"}
    route = respx.get("https://duckhaven.test/api/dashboard-schema").mock(
        return_value=httpx.Response(200, json=schema)
    )

    result = runner.invoke(app, ["dashboard", "schema", "--spec-version", "1"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == schema
    assert route.calls[0].request.url.params["version"] == "1"


# --- Renaming and adopting -------------------------------------------------


@respx.mock
def test_rename_applies_the_renamed_file_to_the_old_name_and_publishes_it(logged_in, tmp_path):
    file = tmp_path / "sales-weekly.dashboard.yml"
    file.write_bytes(SALES)
    route = respx.put(f"{BOARDS}/sales/source").mock(
        return_value=httpx.Response(
            200, json=_answer("sales-weekly", renamed_from="sales", published=True)
        )
    )

    result = runner.invoke(app, ["dashboard", "rename", "sales", str(file)])

    assert result.exit_code == 0, result.output
    assert route.calls[0].request.content == SALES
    assert route.calls[0].request.url.params["publish"] == "true"
    assert "sales-weekly: update, published, renamed from sales" in result.stderr
    assert json.loads(result.stdout)["data"]["file"] == str(file)


@respx.mock
def test_rename_refuses_a_file_still_named_as_before(logged_in, repo):
    result = runner.invoke(app, ["dashboard", "rename", "sales", str(repo / "sales.dashboard.yml")])

    assert result.exit_code == ExitCode.CONFLICT
    assert "still named 'sales'" in result.output
    assert respx.calls.call_count == 0


def _found(name: str = "refunds", **extra):
    respx.get(f"{BOARDS}/Refunds").mock(
        return_value=httpx.Response(200, json={"name": name, "managed": False, **extra})
    )


@respx.mock
def test_adopt_saves_the_newest_file_and_applies_it(logged_in, tmp_path):
    """The file takes the dashboard's own name, whatever case it was asked for in."""
    _found()
    source = respx.get(f"{BOARDS}/refunds/source").mock(
        return_value=httpx.Response(200, content=SALES)
    )
    apply = respx.put(f"{BOARDS}/refunds/source").mock(
        return_value=httpx.Response(200, json=_answer("refunds", adopted=True))
    )

    result = runner.invoke(app, ["dashboard", "adopt", "Refunds", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert source.calls[0].request.url.params["draft"] == "true"
    assert apply.calls[0].request.content == SALES
    assert dict(apply.calls[0].request.url.params) == {"adopt": "true"}
    assert (tmp_path / "refunds.dashboard.yml").read_bytes() == SALES
    assert "refunds: update, adopted" in result.stderr


@respx.mock
def test_adopt_leaves_a_file_already_there_alone(logged_in, tmp_path):
    _found()
    (tmp_path / "refunds.dashboard.yml").write_text("mine\n", encoding="utf-8")

    result = runner.invoke(app, ["dashboard", "adopt", "Refunds", str(tmp_path)])

    assert result.exit_code == ExitCode.CONFLICT
    assert (tmp_path / "refunds.dashboard.yml").read_text(encoding="utf-8") == "mine\n"
    assert respx.calls.call_count == 1


@respx.mock
def test_adopt_refuses_a_dashboard_managed_in_git(logged_in, tmp_path):
    _found(managed=True, source_id="acme/bi")

    result = runner.invoke(app, ["dashboard", "adopt", "Refunds", str(tmp_path)])

    assert result.exit_code == ExitCode.CONFLICT
    assert "managed in git already, from acme/bi" in result.output


@respx.mock
def test_a_refused_adopt_saves_no_file(logged_in, tmp_path):
    _found()
    respx.get(f"{BOARDS}/refunds/source").mock(return_value=httpx.Response(200, content=SALES))
    respx.put(f"{BOARDS}/refunds/source").mock(
        return_value=_refused(403, "http_403", "Owner only.")
    )

    result = runner.invoke(app, ["dashboard", "adopt", "Refunds", str(tmp_path)])

    assert result.exit_code == ExitCode.AUTH
    assert not (tmp_path / "refunds.dashboard.yml").exists()


# --- The rest --------------------------------------------------------------


@respx.mock
def test_publish_sends_the_drafts_version_and_the_message(logged_in):
    respx.get(f"{BOARDS}/sales").mock(return_value=httpx.Response(200, json={"version": 7}))
    route = respx.post(f"{BOARDS}/sales/publish").mock(
        return_value=httpx.Response(200, json={"name": "sales", "version": 8})
    )

    result = runner.invoke(app, ["dashboard", "publish", "sales", "-m", "Weekly view"])

    assert result.exit_code == 0, result.output
    assert json.loads(route.calls[0].request.content) == {
        "base_version": 7,
        "message": "Weekly view",
    }


@respx.mock
def test_list_sends_its_filters(logged_in):
    route = respx.get(BOARDS).mock(
        return_value=httpx.Response(200, json={"items": [], "cursor": None, "has_more": False})
    )
    argv = ["dashboard", "list", "--status", "published", "--source", "git"]
    argv += ["--source-id", "acme/bi", "--uses", "shop.revenue", "-q", "sal"]

    assert runner.invoke(app, argv).exit_code == 0
    assert dict(route.calls[0].request.url.params) == {
        "status": "published",
        "source": "git",
        "source_id": "acme/bi",
        "uses": "shop.revenue",
        "q": "sal",
    }
