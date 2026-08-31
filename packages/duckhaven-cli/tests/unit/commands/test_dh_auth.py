"""`dh auth login|status|logout` and `dh health` / `dh version`.

The login tests are the important ones. They assert the property the server's
cookie-only PAT route exists to guarantee: the session cookie is used for the
exchange and never written to disk.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from typer.testing import CliRunner

from dh import config as config_mod
from dh.errors import ExitCode
from dh.main import app

runner = CliRunner()

HOST = "https://duckhaven.test"
API = f"{HOST}/api"
ME = {"email": "analyst@example.com", "name": "Analyst", "role": "user"}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setenv("DH_CONFIG_FILE", str(path))
    for var in ("DH_HOST", "DH_TOKEN", "DH_WORKSPACE", "DH_CATALOG", "DH_AGENT", "DH_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return path


@pytest.fixture
def logged_in(cfg):
    cfg.write_text(
        'default_profile = "default"\n\n[profile.default]\n'
        f'host = "{HOST}"\ntoken = "dh_pat_stored"\nworkspace = "analytics"\n',
        encoding="utf-8",
    )
    os.chmod(cfg, 0o600)
    return cfg


def _mock_login(*, methods=None, pat_status=201):
    respx.get(f"{API}/auth/methods").mock(
        return_value=httpx.Response(
            200, json=methods or {"local": True, "ldap": False, "oidc_providers": []}
        )
    )
    respx.post(f"{API}/auth/login").mock(
        return_value=httpx.Response(200, json=ME, headers={"set-cookie": "session=cookie-value"})
    )
    respx.post(f"{API}/me/pats").mock(
        return_value=httpx.Response(
            pat_status,
            json={"id": "1", "token": "dh_pat_minted", "expires_at": "2026-11-29T00:00:00Z"}
            if pat_status == 201
            else {"error": "not_found", "message": "Not Found"},
        )
    )
    respx.post(f"{API}/auth/logout").mock(return_value=httpx.Response(204))
    respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json=ME))


# --- login -----------------------------------------------------------------


@respx.mock
def test_login_mints_a_token_and_writes_a_0600_profile(cfg):
    _mock_login()
    result = runner.invoke(
        app,
        ["--format", "json", "auth", "login", "--host", HOST, "--email", ME["email"]],
        input="hunter2\n",
    )
    assert result.exit_code == 0
    saved = config_mod.load()
    assert saved.profiles["default"].token == "dh_pat_minted"
    assert saved.profiles["default"].host == HOST
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


@respx.mock
def test_login_never_writes_the_session_cookie_to_disk(cfg):
    """The whole point of the server's cookie-only PAT route."""
    _mock_login()
    runner.invoke(app, ["auth", "login", "--host", HOST, "--email", ME["email"]], input="pw\n")
    assert "cookie-value" not in cfg.read_text(encoding="utf-8")


@respx.mock
def test_login_signs_out_after_minting(cfg):
    """Leaving a live session behind would be a credential nobody is tracking."""
    _mock_login()
    runner.invoke(app, ["auth", "login", "--host", HOST, "--email", ME["email"]], input="pw\n")
    assert any(call.request.url.path.endswith("/auth/logout") for call in respx.calls)


@respx.mock
def test_login_verifies_the_token_before_storing_it(cfg):
    """A profile holding a token that does not work is worse than no profile."""
    _mock_login()
    respx.get(f"{API}/me").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized", "message": "nope"})
    )
    result = runner.invoke(
        app, ["auth", "login", "--host", HOST, "--email", ME["email"]], input="pw\n"
    )
    assert result.exit_code == ExitCode.AUTH
    assert config_mod.load().profiles == {}


@respx.mock
def test_login_with_a_supplied_token_skips_the_password_exchange(cfg):
    respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json=ME))
    result = runner.invoke(app, ["auth", "login", "--host", HOST, "--token", "dh_pat_given"])
    assert result.exit_code == 0
    assert config_mod.load().profiles["default"].token == "dh_pat_given"
    assert not any(c.request.url.path.endswith("/auth/login") for c in respx.calls)


@respx.mock
def test_login_on_an_oidc_only_deployment_explains_the_alternative(cfg):
    _mock_login(methods={"local": False, "ldap": False, "oidc_providers": [{"id": "okta"}]})
    result = runner.invoke(app, ["auth", "login", "--host", HOST])
    assert result.exit_code == ExitCode.AUTH
    assert "okta" in result.output
    assert "--token" in result.output


@respx.mock
def test_login_against_a_server_without_me_pats_says_what_to_do(cfg):
    """A bare 404 here is not something the reader can act on."""
    _mock_login(pat_status=404)
    result = runner.invoke(
        app, ["auth", "login", "--host", HOST, "--email", ME["email"]], input="pw\n"
    )
    assert result.exit_code == ExitCode.AUTH
    assert "too old" in result.output
    assert "--token" in result.output


@respx.mock
def test_login_names_a_non_default_profile(cfg):
    _mock_login()
    runner.invoke(
        app,
        ["auth", "login", "--host", HOST, "--email", ME["email"], "--name", "prod"],
        input="pw\n",
    )
    assert "prod" in config_mod.load().profiles


# --- status ----------------------------------------------------------------


def _mock_pats(rows=None):
    return respx.get(f"{API}/me/pats").mock(return_value=httpx.Response(200, json=rows or []))


@respx.mock
def test_status_reports_the_user_and_server_version(logged_in):
    _mock_pats()
    respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json=ME))
    respx.get(f"{API}/version").mock(
        return_value=httpx.Response(200, json={"version": "1.4.0", "api_version": 2})
    )
    result = runner.invoke(app, ["--format", "json", "auth", "status"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)["data"]
    assert data["user"] == ME["email"]
    assert data["api_version"] == 2


@respx.mock
def test_status_tolerates_a_server_without_a_version_endpoint(logged_in):
    _mock_pats()
    respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json=ME))
    respx.get(f"{API}/version").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    result = runner.invoke(app, ["--format", "json", "auth", "status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["server_version"] is None


def test_status_without_a_profile_names_every_way_to_set_a_host(cfg):
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == ExitCode.AUTH
    assert "DH_HOST" in result.output


# --- logout ----------------------------------------------------------------


def test_logout_clears_only_the_token(logged_in):
    result = runner.invoke(app, ["auth", "logout"])
    assert result.exit_code == 0
    profile = config_mod.load().profiles["default"]
    assert profile.token is None
    assert profile.host == HOST
    assert profile.workspace == "analytics"


def test_logout_says_the_token_is_still_valid(logged_in):
    """`dh` cannot revoke it: the API has no self-revocation endpoint."""
    assert "remains valid" in runner.invoke(app, ["auth", "logout"]).output


# --- health and version ----------------------------------------------------


@respx.mock
def test_health_reports_each_check_separately(logged_in):
    respx.get(f"{API}/healthz").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx.get(f"{API}/readyz").mock(return_value=httpx.Response(503, json={"error": "unavailable"}))
    respx.get(f"{API}/maintenance/health").mock(
        return_value=httpx.Response(200, json={"status": "green"})
    )
    respx.get(f"{API}/workspaces/analytics/health").mock(
        return_value=httpx.Response(200, json={"status": "green"})
    )
    result = runner.invoke(app, ["--format", "json", "health"])
    assert result.exit_code == 0
    checks = {c["check"]: c for c in json.loads(result.stdout)["data"]}
    # One failing dependency must not hide the state of the others.
    assert checks["liveness"]["ok"] is True
    assert checks["readiness"]["ok"] is False
    assert checks["workspace"]["ok"] is True


@respx.mock
def test_version_reports_both_sides(logged_in):
    respx.get(f"{API}/version").mock(
        return_value=httpx.Response(200, json={"version": "1.4.0", "api_version": 2})
    )
    data = json.loads(runner.invoke(app, ["--format", "json", "version"]).stdout)["data"]
    assert data["server"] == "1.4.0"
    assert data["api_version"] == 2
    assert data["cli"]


def test_version_works_with_no_configuration_at_all(cfg):
    """It is what people run first when something is wrong."""
    result = runner.invoke(app, ["--format", "json", "version"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["server"] is None


@respx.mock
def test_version_still_reports_the_cli_when_the_server_is_unreachable(logged_in):
    respx.get(f"{API}/version").mock(side_effect=httpx.ConnectError("refused"))
    result = runner.invoke(app, ["--format", "json", "version"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["cli"]


# --- Token listing, revocation and expiry warnings -------------------------


def _in(days: int) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(days=days)).isoformat()


@respx.mock
def test_status_warns_before_a_token_expires(logged_in):
    """The first symptom of an expired token is a 401 nobody was watching for."""
    respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json=ME))
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
    _mock_pats([{"id": "p1", "expires_at": _in(3), "current": True}])
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    assert "expires in 3 days" in result.output
    assert "dh auth login" in result.output


@respx.mock
def test_status_is_quiet_when_the_token_has_plenty_of_life(logged_in):
    respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json=ME))
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
    _mock_pats([{"id": "p1", "expires_at": _in(60), "current": True}])
    assert "expires in" not in runner.invoke(app, ["auth", "status"]).output


@respx.mock
def test_status_says_so_when_the_token_has_already_expired(logged_in):
    respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json=ME))
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
    _mock_pats([{"id": "p1", "expires_at": _in(-1), "current": True}])
    assert "has expired" in runner.invoke(app, ["auth", "status"]).output


@respx.mock
def test_status_ignores_tokens_that_are_not_the_current_one(logged_in):
    """Another token expiring tomorrow is not this session's problem."""
    respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json=ME))
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
    _mock_pats([{"id": "other", "expires_at": _in(1), "current": False}])
    assert "expires in" not in runner.invoke(app, ["auth", "status"]).output


@respx.mock
def test_status_still_works_against_a_server_without_the_listing(logged_in):
    """A missing warning, not a failure."""
    respx.get(f"{API}/me").mock(return_value=httpx.Response(200, json=ME))
    respx.get(f"{API}/version").mock(return_value=httpx.Response(200, json={"version": "1"}))
    respx.get(f"{API}/me/pats").mock(
        return_value=httpx.Response(404, json={"error": "not_found", "message": "x"})
    )
    result = runner.invoke(app, ["--format", "json", "auth", "status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"]["token_expires_at"] is None


@respx.mock
def test_auth_tokens_lists_metadata_and_never_a_secret(logged_in):
    _mock_pats([{"id": "p1", "created_at": _in(-30), "expires_at": _in(60), "current": True}])
    result = runner.invoke(app, ["--format", "json", "auth", "tokens"])
    assert result.exit_code == 0
    rows = json.loads(result.stdout)["data"]
    assert rows[0]["current"] is True
    assert "token" not in rows[0]


@respx.mock
def test_auth_revoke_deletes_by_id(logged_in):
    route = respx.delete(f"{API}/me/pats/p1").mock(return_value=httpx.Response(204))
    assert runner.invoke(app, ["auth", "revoke", "p1"]).exit_code == 0
    assert route.called
