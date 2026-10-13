"""`dh admin` and `dh api`."""

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
API = f"{HOST}/api"
SA = "11111111-1111-1111-1111-111111111111"
AGENT = "22222222-2222-2222-2222-222222222222"
USER = "33333333-3333-3333-3333-333333333333"


def _data(result):
    return json.loads(result.stdout)["data"]


# --- Service accounts and tokens -------------------------------------------


@respx.mock
def test_service_account_create_defaults_to_no_permissions(logged_in):
    """A new account must never be accidentally an admin."""
    route = respx.post(f"{API}/admin/service-accounts").mock(
        return_value=httpx.Response(201, json={"id": SA})
    )
    runner.invoke(app, ["admin", "service-account", "create", "ci"])
    assert json.loads(route.calls[0].request.content) == {"name": "ci", "role": "user"}


@respx.mock
def test_service_account_disable(logged_in):
    route = respx.patch(f"{API}/admin/service-accounts/{SA}").mock(
        return_value=httpx.Response(200, json={"id": SA})
    )
    runner.invoke(app, ["admin", "service-account", "update", SA, "--inactive"])
    assert json.loads(route.calls[0].request.content) == {"is_active": False}


@respx.mock
def test_pat_issue_warns_the_secret_is_shown_once(logged_in):
    respx.post(f"{API}/admin/service-accounts/{SA}/pats").mock(
        return_value=httpx.Response(201, json={"id": "p1", "token": "dh_pat_new"})
    )
    result = runner.invoke(app, ["--format", "json", "admin", "pat", "issue", SA])
    assert result.exit_code == 0
    assert _data(result)["token"] == "dh_pat_new"
    assert "cannot be shown again" in result.output


@respx.mock
def test_pat_issue_translates_zero_days_to_never_expires(logged_in):
    route = respx.post(f"{API}/admin/service-accounts/{SA}/pats").mock(
        return_value=httpx.Response(201, json={})
    )
    runner.invoke(app, ["admin", "pat", "issue", SA, "--expires-in-days", "0"])
    assert json.loads(route.calls[0].request.content) == {"expires_in_days": None}


@respx.mock
def test_pat_revoke(logged_in):
    route = respx.delete(f"{API}/admin/service-accounts/{SA}/pats/p1").mock(
        return_value=httpx.Response(204)
    )
    assert runner.invoke(app, ["admin", "pat", "revoke", SA, "p1"]).exit_code == 0
    assert route.called


# --- Users -----------------------------------------------------------------


@respx.mock
def test_user_create_takes_the_password_from_a_prompt(logged_in):
    """A password on the command line lands in shell history."""
    route = respx.post(f"{API}/admin/users").mock(return_value=httpx.Response(201, json={}))
    result = runner.invoke(
        app, ["admin", "user", "create", "new@example.com", "--name", "New"], input="secret\n"
    )
    assert result.exit_code == 0
    assert json.loads(route.calls[0].request.content)["password"] == "secret"


@respx.mock
def test_user_set_workspace_role(logged_in):
    route = respx.put(f"{API}/admin/users/{USER}/workspaces/analytics").mock(
        return_value=httpx.Response(200, json={})
    )
    runner.invoke(app, ["admin", "user", "set-workspace-role", USER, "analytics", "writer"])
    assert json.loads(route.calls[0].request.content) == {"role": "writer"}


@respx.mock
def test_user_revoke_sessions(logged_in):
    route = respx.post(f"{API}/admin/users/{USER}/revoke-sessions").mock(
        return_value=httpx.Response(204)
    )
    assert runner.invoke(app, ["admin", "user", "revoke-sessions", USER]).exit_code == 0
    assert route.called


# --- Agents ----------------------------------------------------------------


@respx.mock
def test_agent_bootstrap_warns_it_is_single_use(logged_in):
    respx.post(f"{API}/admin/agents/bootstrap").mock(
        return_value=httpx.Response(201, json={"token": "dh_boot_x", "url": "wss://..."})
    )
    result = runner.invoke(app, ["admin", "agent", "bootstrap"])
    assert "single-use" in result.output


@respx.mock
@pytest.mark.parametrize("action", ["restart", "terminate", "disconnect"])
def test_the_agent_lifecycle_transitions(logged_in, action):
    route = respx.post(f"{API}/admin/agents/{AGENT}/{action}").mock(
        return_value=httpx.Response(202, json={"id": AGENT})
    )
    assert runner.invoke(app, ["admin", "agent", action, AGENT]).exit_code == 0
    assert route.called


@respx.mock
def test_elastic_create_sends_the_shape(logged_in):
    route = respx.post(f"{API}/admin/agents/elastic").mock(
        return_value=httpx.Response(202, json={"id": AGENT})
    )
    runner.invoke(app, ["admin", "agent", "elastic-create", "--cpu", "4", "--memory-gb", "8"])
    assert json.loads(route.calls[0].request.content) == {"cpu": 4.0, "memory_gb": 8.0}


@respx.mock
def test_elastic_create_picks_a_runtime_and_can_allow_beta(logged_in):
    route = respx.post(f"{API}/admin/agents/elastic").mock(
        return_value=httpx.Response(202, json={"id": AGENT})
    )
    runner.invoke(
        app,
        ["admin", "agent", "elastic-create", "--cpu", "2", "--memory-gb", "4"]
        + ["--runtime", "1.6", "--allow-beta"],
    )
    body = json.loads(route.calls[0].request.content)
    assert body["runtime_id"] == "1.6"
    assert body["allow_beta"] is True


@respx.mock
def test_a_retired_runtime_is_a_conflict_with_the_servers_reason(logged_in):
    """409 runtime_retired comes back in the v2 envelope; the message must survive."""
    respx.post(f"{API}/admin/agents/elastic").mock(
        return_value=httpx.Response(
            409, json={"error": "runtime_retired", "message": "Runtime 1.4 is retired."}
        )
    )
    result = runner.invoke(
        app, ["admin", "agent", "elastic-create", "--cpu", "2", "--memory-gb", "4"]
    )
    assert result.exit_code == ExitCode.CONFLICT
    assert "retired" in result.stderr


@respx.mock
def test_monitoring_takes_a_preset_window(logged_in):
    route = respx.get(f"{API}/admin/agents/{AGENT}/monitoring").mock(
        return_value=httpx.Response(200, json={})
    )
    runner.invoke(app, ["admin", "agent", "monitoring", AGENT, "--window", "24h"])
    assert route.calls[0].request.url.params["window"] == "24h"
    assert "start" not in route.calls[0].request.url.params


@respx.mock
def test_monitoring_takes_a_custom_range(logged_in):
    route = respx.get(f"{API}/admin/agents/{AGENT}/monitoring").mock(
        return_value=httpx.Response(200, json={})
    )
    runner.invoke(
        app,
        ["admin", "agent", "monitoring", AGENT]
        + ["--start", "2026-10-12T00:00:00Z", "--end", "2026-10-12T06:00:00Z"],
    )
    params = route.calls[0].request.url.params
    assert params["start"] == "2026-10-12T00:00:00Z"
    assert params["end"] == "2026-10-12T06:00:00Z"
    assert "window" not in params


@pytest.mark.parametrize(
    "extra",
    [
        ["--start", "2026-10-12T00:00:00Z"],
        ["--end", "2026-10-12T00:00:00Z"],
        ["--window", "1h", "--start", "2026-10-12T00:00:00Z", "--end", "2026-10-12T01:00:00Z"],
    ],
)
@respx.mock
def test_monitoring_refuses_an_ambiguous_range_before_asking(logged_in, extra):
    route = respx.get(f"{API}/admin/agents/{AGENT}/monitoring")
    result = runner.invoke(app, ["admin", "agent", "monitoring", AGENT, *extra])
    assert result.exit_code == ExitCode.CONFLICT
    assert not route.called


@respx.mock
def test_agent_queries_sends_the_range_filters_and_sort(logged_in):
    route = respx.get(f"{API}/admin/agents/{AGENT}/queries").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "q1"}], "cursor": None, "has_more": False}
        )
    )
    result = runner.invoke(
        app,
        ["--format", "json", "admin", "agent", "queries", AGENT]
        + ["--start", "2026-10-12T00:00:00Z", "--end", "2026-10-12T06:00:00Z"]
        + ["--status", "failed", "--status", "done", "--sort", "peak_memory", "--dir", "asc"]
        + ["--limit", "5"],
    )
    assert result.exit_code == 0, result.output
    params = route.calls[0].request.url
    assert params.params.get_list("status") == ["failed", "done"]
    assert params.params["sort"] == "peak_memory"
    assert params.params["dir"] == "asc"
    assert params.params["limit"] == "5"
    assert _data(result) == [{"id": "q1"}]


def test_agent_queries_needs_a_range(logged_in):
    result = runner.invoke(app, ["admin", "agent", "queries", AGENT])
    assert result.exit_code == ExitCode.USAGE


@respx.mock
def test_agent_revoke_credential(logged_in):
    route = respx.delete(f"{API}/admin/agents/{AGENT}/credential").mock(
        return_value=httpx.Response(204)
    )
    assert runner.invoke(app, ["admin", "agent", "revoke-credential", AGENT]).exit_code == 0
    assert route.called


# --- Storage and maintenance -----------------------------------------------


@respx.mock
def test_storage_health_posts(logged_in):
    route = respx.post(f"{API}/admin/storage-backends/b1/health").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    assert runner.invoke(app, ["admin", "storage", "health", "b1"]).exit_code == 0
    assert route.called


@respx.mock
def test_maintenance_scan(logged_in):
    route = respx.post(f"{API}/admin/maintenance/scan").mock(
        return_value=httpx.Response(200, json={"scanned": 3})
    )
    assert runner.invoke(app, ["admin", "maintenance", "scan"]).exit_code == 0
    assert route.called


@respx.mock
def test_maintenance_recommendations_filter_and_list(logged_in):
    route = respx.get(f"{API}/maintenance/recommendations").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"id": "r1", "apply_status": None}],
                "cursor": None,
                "has_more": False,
            },
        )
    )
    result = runner.invoke(
        app,
        ["--format", "json", "admin", "maintenance", "recommendations"]
        + ["--status", "open", "--severity", "high", "--severity", "medium"],
    )
    assert result.exit_code == 0
    url = route.calls[0].request.url
    assert url.params.get_list("severity") == ["high", "medium"]
    assert _data(result)[0]["id"] == "r1"


@respx.mock
def test_maintenance_apply_refuses_without_confirmation(logged_in):
    """It rewrites or deletes data, so a pipeline must say --yes."""
    route = respx.post(f"{API}/maintenance/recommendations/{AGENT}/apply")
    result = runner.invoke(app, ["admin", "maintenance", "apply", AGENT])
    assert result.exit_code != 0
    assert not route.called


@respx.mock
def test_maintenance_apply_for_a_kind_that_cannot_is_the_servers_refusal(logged_in):
    respx.post(f"{API}/maintenance/recommendations/{AGENT}/apply").mock(
        return_value=httpx.Response(
            403,
            json={
                "error": "forbidden",
                "message": "Applying maintenance requires the 'maintenance:manage' permission.",
            },
        )
    )
    result = runner.invoke(app, ["admin", "maintenance", "apply", AGENT, "--yes"])
    assert result.exit_code == ExitCode.AUTH
    assert "maintenance:manage" in result.stderr


# --- dh api ----------------------------------------------------------------


@respx.mock
def test_api_get_reaches_any_path(logged_in):
    """The escape hatch is what makes the coverage cut honest."""
    respx.get(f"{API}/workspaces/analytics/assistant/status").mock(
        return_value=httpx.Response(200, json={"enabled": True})
    )
    result = runner.invoke(
        app, ["--format", "json", "api", "get", "workspaces/analytics/assistant/status"]
    )
    assert result.exit_code == 0
    assert _data(result) == {"enabled": True}


@respx.mock
def test_api_post_sends_an_inline_body(logged_in):
    route = respx.post(f"{API}/thing").mock(return_value=httpx.Response(200, json={}))
    runner.invoke(app, ["api", "post", "thing", "-d", '{"a": 1}'])
    assert json.loads(route.calls[0].request.content) == {"a": 1}


@respx.mock
def test_api_post_reads_a_body_from_a_file(logged_in, tmp_path):
    body = tmp_path / "body.json"
    body.write_text('{"b": 2}', encoding="utf-8")
    route = respx.post(f"{API}/thing").mock(return_value=httpx.Response(200, json={}))
    runner.invoke(app, ["api", "post", "thing", "-d", f"@{body}"])
    assert json.loads(route.calls[0].request.content) == {"b": 2}


@respx.mock
def test_api_repeats_a_query_parameter(logged_in):
    """`status` is repeatable server-side, so the escape hatch must allow it."""
    route = respx.get(f"{API}/thing").mock(return_value=httpx.Response(200, json=[]))
    runner.invoke(app, ["api", "get", "thing", "-p", "status=failed", "-p", "status=done"])
    assert route.calls[0].request.url.params.get_list("status") == ["failed", "done"]


def test_api_rejects_a_malformed_body(logged_in):
    result = runner.invoke(app, ["api", "post", "thing", "-d", "{oops"])
    assert result.exit_code == ExitCode.CONFLICT
    assert "not valid JSON" in result.output


def test_api_rejects_a_malformed_parameter(logged_in):
    result = runner.invoke(app, ["api", "get", "thing", "-p", "oops"])
    assert result.exit_code == ExitCode.CONFLICT
    assert "name=value" in result.output


def test_api_reports_a_missing_body_file(logged_in):
    result = runner.invoke(app, ["api", "post", "thing", "-d", "@/nonexistent.json"])
    assert result.exit_code == ExitCode.CONFLICT
    assert "No such file" in result.output


@respx.mock
def test_api_errors_still_use_the_taxonomy(logged_in):
    """The path is yours; the error contract is not."""
    respx.get(f"{API}/nope").mock(
        return_value=httpx.Response(404, json={"error": "not_found", "message": "gone"})
    )
    result = runner.invoke(app, ["api", "get", "nope"])
    assert result.exit_code == ExitCode.NOT_FOUND


# --- dh api: bodies that are not JSON --------------------------------------


@respx.mock
def test_api_returns_a_non_json_body_verbatim(logged_in):
    """`/metrics` is Prometheus text and the assistant route streams SSE. Both
    are reachable only here, so a raw body is the answer, not a crash."""
    respx.get(f"{API}/metrics").mock(
        return_value=httpx.Response(
            200, text="# HELP up\nup 1\n", headers={"content-type": "text/plain"}
        )
    )
    result = runner.invoke(app, ["--format", "json", "api", "get", "metrics"])
    assert result.exit_code == 0
    assert "up 1" in json.loads(result.stdout)["data"]
