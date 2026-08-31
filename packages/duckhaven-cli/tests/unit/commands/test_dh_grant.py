"""`dh grant` — catalog access control."""

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
WS = f"{HOST}/api/workspaces/analytics"
CATALOG = f"{WS}/catalogs/main"
UID = "12345678-1234-1234-1234-123456789abc"

PAYLOAD = {
    "access_mode": "scoped",
    "grants": [{"id": "g1", "user_id": UID, "tier": "reader", "schema_name": None}],
    "principals": [
        {"user_id": UID, "name": "Analyst", "email": "analyst@example.com", "role": "user"},
        {"user_id": "other", "name": "Bot", "email": "bot@example.com", "role": "user"},
    ],
}


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


def _mock_list():
    return respx.get(f"{CATALOG}/grants").mock(return_value=httpx.Response(200, json=PAYLOAD))


# --- list ------------------------------------------------------------------


@respx.mock
def test_grant_list_shows_the_grants_and_notes_the_mode(logged_in):
    _mock_list()
    result = runner.invoke(app, ["--format", "json", "grant", "list"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["data"][0]["tier"] == "reader"
    assert "access mode: scoped" in result.output


@respx.mock
def test_grant_list_principals_shows_the_candidates(logged_in):
    _mock_list()
    result = runner.invoke(app, ["--format", "json", "grant", "list", "--principals"])
    emails = [p["email"] for p in json.loads(result.stdout)["data"]]
    assert emails == ["analyst@example.com", "bot@example.com"]


# --- set -------------------------------------------------------------------


@respx.mock
def test_grant_set_resolves_an_email_to_a_user_id(logged_in):
    """Requiring a pasted UUID would waste the advantage this has over curl."""
    _mock_list()
    route = respx.put(f"{CATALOG}/grants").mock(return_value=httpx.Response(200, json={}))
    result = runner.invoke(
        app, ["grant", "set", "--user", "analyst@example.com", "--tier", "reader"]
    )
    assert result.exit_code == 0
    assert json.loads(route.calls[0].request.content) == {"user_id": UID, "tier": "reader"}


@respx.mock
def test_grant_set_accepts_a_user_id_directly(logged_in):
    _mock_list()
    route = respx.put(f"{CATALOG}/grants").mock(return_value=httpx.Response(200, json={}))
    runner.invoke(app, ["grant", "set", "--user", UID, "--tier", "writer"])
    assert json.loads(route.calls[0].request.content)["user_id"] == UID


@respx.mock
def test_grant_set_narrows_to_a_schema_and_table(logged_in):
    _mock_list()
    route = respx.put(f"{CATALOG}/grants").mock(return_value=httpx.Response(200, json={}))
    runner.invoke(
        app,
        [
            "grant",
            "set",
            "--user",
            UID,
            "--tier",
            "reader",
            "--schema",
            "sales",
            "--table",
            "orders",
        ],
    )
    body = json.loads(route.calls[0].request.content)
    assert body["schema_name"] == "sales"
    assert body["table_name"] == "orders"


def test_a_table_without_a_schema_is_refused_before_the_round_trip(logged_in):
    result = runner.invoke(
        app, ["grant", "set", "--user", UID, "--tier", "reader", "--table", "orders"]
    )
    assert result.exit_code == ExitCode.CONFLICT
    assert "--schema" in result.output


@respx.mock
def test_an_unknown_principal_lists_the_known_ones(logged_in):
    _mock_list()
    result = runner.invoke(
        app, ["grant", "set", "--user", "nobody@example.com", "--tier", "reader"]
    )
    assert result.exit_code == ExitCode.CONFLICT
    assert "analyst@example.com" in result.output


# --- remove and access-mode ------------------------------------------------


@respx.mock
def test_grant_remove(logged_in):
    route = respx.delete(f"{CATALOG}/grants/g1").mock(return_value=httpx.Response(204))
    assert runner.invoke(app, ["grant", "remove", "g1"]).exit_code == 0
    assert route.called


@respx.mock
def test_access_mode_switch(logged_in):
    route = respx.patch(f"{CATALOG}/access-mode").mock(
        return_value=httpx.Response(200, json={"access_mode": "open"})
    )
    assert runner.invoke(app, ["grant", "access-mode", "open"]).exit_code == 0
    assert json.loads(route.calls[0].request.content) == {"access_mode": "open"}


def test_an_invalid_access_mode_is_refused_locally(logged_in):
    result = runner.invoke(app, ["grant", "access-mode", "public"])
    assert result.exit_code == ExitCode.CONFLICT
    assert "open or scoped" in result.output
