"""Every remaining command reaches the endpoint it claims to.

Most commands are three lines: resolve settings, call one route, emit. The thing
that can actually be wrong is the URL, so this asserts that for the whole tree in
one table rather than repeating a near-identical test per command. A typo in a
path fails here; behaviour worth more than a URL check has its own test file.
"""

from __future__ import annotations

import os

import httpx
import pytest
import respx
from typer.testing import CliRunner

from dh.main import app

runner = CliRunner()

HOST = "https://duckhaven.test"
API = f"{HOST}/api"
WS = f"{API}/workspaces/analytics"
CAT = f"{WS}/catalogs/main"
ID = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def logged_in(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        'default_profile = "default"\n\n[profile.default]\n'
        f'host = "{HOST}"\ntoken = "dh_pat_x"\nworkspace = "analytics"\ncatalog = "main"\n',
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    monkeypatch.setenv("DH_CONFIG_FILE", str(path))
    for var in ("DH_HOST", "DH_TOKEN", "DH_WORKSPACE", "DH_CATALOG", "DH_AGENT", "DH_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return path


#: (argv, method, url). One row per command whose whole job is one request.
ROUTES = [
    # workspaces
    (["workspace", "get"], "GET", WS),
    (["workspace", "update", "--name", "New"], "PATCH", WS),
    (["workspace", "delete", "analytics"], "DELETE", WS),
    (["workspace", "member", "add", ID], "POST", f"{WS}/members"),
    # catalogs
    (["catalog", "create", "new"], "POST", f"{WS}/catalogs"),
    (["catalog", "drop", ID], "DELETE", f"{API}/catalogs/{ID}"),
    (["catalog", "refresh-stats"], "POST", f"{CAT}/refresh-stats"),
    # schemas
    (["schema", "drop", "sales"], "DELETE", f"{CAT}/schemas/sales"),
    # saved queries and schedules
    (["saved-query", "list"], "GET", f"{WS}/saved-queries"),
    (["schedule", "list"], "GET", f"{WS}/schedules"),
    (["schedule", "delete", ID], "DELETE", f"{WS}/schedules/{ID}"),
    (["schedule", "runs"], "GET", f"{WS}/schedule-runs"),
    # sessions
    (["session", "list"], "GET", f"{WS}/sql/sessions"),
    # semantic
    (["semantic", "model", "list"], "GET", f"{WS}/semantic/models"),
    (["semantic", "model", "get", "m"], "GET", f"{WS}/semantic/models/m"),
    (
        ["semantic", "relationship", "remove", "m", "r"],
        "DELETE",
        f"{WS}/semantic/models/m/relationships/r",
    ),
    # admin: service accounts and tokens
    (["admin", "service-account", "list"], "GET", f"{API}/admin/service-accounts"),
    (["admin", "service-account", "delete", ID], "DELETE", f"{API}/admin/service-accounts/{ID}"),
    (["admin", "pat", "list", ID], "GET", f"{API}/admin/service-accounts/{ID}/pats"),
    # admin: users
    (["admin", "user", "list"], "GET", f"{API}/admin/users"),
    (["admin", "user", "update", ID, "--role", "admin"], "PATCH", f"{API}/admin/users/{ID}"),
    (["admin", "user", "workspaces", ID], "GET", f"{API}/admin/users/{ID}/workspaces"),
    (
        ["admin", "user", "remove-from-workspace", ID, "analytics"],
        "DELETE",
        f"{API}/admin/users/{ID}/workspaces/analytics",
    ),
    # admin: agents
    (["admin", "agent", "list"], "GET", f"{API}/admin/agents"),
    (["admin", "agent", "get", ID], "GET", f"{API}/admin/agents/{ID}"),
    (["admin", "agent", "metrics"], "GET", f"{API}/admin/agents/metrics"),
    (["admin", "agent", "monitoring", ID], "GET", f"{API}/admin/agents/{ID}/monitoring"),
    (["admin", "agent", "access", ID], "GET", f"{API}/admin/agents/{ID}/access"),
    (["admin", "agent", "compute-options"], "GET", f"{API}/admin/agents/compute-options"),
    (["admin", "agent", "delete", ID], "DELETE", f"{API}/admin/agents/{ID}"),
    # admin: storage and maintenance
    (["admin", "storage", "list"], "GET", f"{API}/admin/storage-backends"),
    (["admin", "maintenance", "policy"], "GET", f"{API}/admin/maintenance/policy"),
]


@respx.mock
@pytest.mark.parametrize(
    ("argv", "method", "url"), ROUTES, ids=lambda v: v if isinstance(v, str) else None
)
def test_each_command_calls_the_endpoint_it_claims(logged_in, argv, method, url):
    route = respx.request(method, url).mock(return_value=httpx.Response(200, json={}))
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert route.called, f"{' '.join(argv)} did not call {method} {url}"
